# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from python.srv.core import storage_tool
from python.srv.core.mongo_db_wrapper import BACKUPS, JOBS, REPOSITORIES
from python.srv.core.storage_tool import StorageToolError, retention_till_text
from python.srv.core.subprocess_util import CompletedProcess
from python.srv.services import abr_v41

MANIFEST_DATA_OBJECTS_ABSENT = (
    "Error: putObjectRetention: 'lab/p1/manifest.abrd': NoSuchKey: The specified key does not exist.\n"
    "Error: putObjectRetention: 'lab/p1/manifest.abri': NoSuchKey: The specified key does not exist.\n"
    "Error: failed to update the retention of backup 'manifest'\n"
)
SHORTENING_REFUSAL = (
    "Error: putObjectRetention: 'lab/p1/01-vm.vmdk.abri': InvalidRequest: Object is WORM protected and cannot be overwritten\n"
    "Error: putObjectRetention: 'lab/p1/01-vm.vmdk.abrd': InvalidRequest: Object is WORM protected and cannot be overwritten\n"
    "Error: failed to update the retention of backup '01-vm.vmdk'\n"
)
MANIFEST_REFUSAL = (
    "Error: putObjectRetention: 'lab/p1/manifest.abrm': InvalidRequest: Object is WORM protected and cannot be overwritten\n"
    "Error: failed to update the retention of backup 'manifest'\n"
)


def _repository(mode: str = "governance") -> dict:
    return {
        "id": "repo-1",
        "name": "Locked",
        "type": "s3",
        "s3": {"bucket": "bucket", "region": "us-east-1", "immutability": {"mode": mode, "retention_days": 5}},
    }


def _json_result(payload: dict) -> str:
    return json.dumps(payload)


def _period(command: list[str]) -> list[str]:
    for option in ("--retention-till", "--retention"):
        if option in command:
            return [option, command[command.index(option) + 1]]
    return []


class LockOutcomeTests(unittest.TestCase):
    def _tool(self, refusals: list[str | None], info: dict | None = None):
        calls: list[list[str]] = []
        answers = iter(refusals)

        def run(command, **_kwargs):
            calls.append(list(command))
            if command[0] == "retention":
                stderr = next(answers)
                if stderr is None:
                    body = {"command": "retention", "status": "ok", "immutability": "governance"}
                    return CompletedProcess(args=command, returncode=0, stdout=_json_result(body), stderr="")
                return CompletedProcess(args=command, returncode=1, stdout="", stderr=stderr)
            if command[0] == "info" and info is not None:
                return CompletedProcess(args=command, returncode=0, stdout=_json_result(info), stderr="")
            return CompletedProcess(args=command, returncode=1, stdout="", stderr="Error: failed to open backup")

        return calls, run

    def _apply(self, run, backup_name: str, *, mode: str = "governance", **period):
        with (
            patch.object(storage_tool, "default_disk_tool_path", return_value=Path(__file__)),
            patch.object(storage_tool, "disk_tool_supports_retention_till", return_value=True),
            patch.object(
                storage_tool,
                "run_disk_tool_process",
                side_effect=lambda _runner, command, **kwargs: run(command[1:], **kwargs),
            ),
            self.assertLogs("python.srv.core.storage_tool", level="DEBUG") as captured,
        ):
            try:
                result = storage_tool.apply_repository_immutability(_repository(mode), "lab/p1", backup_name, **period)
            except StorageToolError as exc:
                result = exc
        warnings = [line for line in captured.output if line.startswith(("WARNING", "ERROR"))]
        return result, warnings, captured.output

    def _info(self, days: int, mode: str = "governance") -> dict:
        return {"command": "info", "status": "ok", "immutability": mode, "retention_days": days}

    def test_a_manifest_held_through_its_metadata_object_is_not_a_failure(self) -> None:
        _calls, run = self._tool([MANIFEST_DATA_OBJECTS_ABSENT])

        result, warnings, output = self._apply(run, "manifest")

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["metadata_only"])
        self.assertEqual(warnings, [])
        self.assertTrue(any(line.startswith("INFO") and "retention manifest" in line and "done" in line for line in output))

    def test_a_disk_whose_data_object_is_missing_still_warns(self) -> None:
        _calls, run = self._tool([MANIFEST_DATA_OBJECTS_ABSENT.replace("manifest", "01-vm.vmdk")])

        _result, warnings, _output = self._apply(run, "01-vm.vmdk")

        self.assertEqual(len(warnings), 1)
        self.assertIn("01-vm.vmdk.abrd", warnings[0])

    def test_a_lock_more_days_longer_than_asked_is_left_as_it_is(self) -> None:
        calls, run = self._tool([SHORTENING_REFUSAL], info=self._info(3))

        result, warnings, output = self._apply(run, "01-vm.vmdk", retention_days=1)

        self.assertEqual([command[0] for command in calls], ["retention", "info"])
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["held_longer"])
        self.assertFalse(result["relocked"])
        self.assertEqual(warnings, [])
        self.assertTrue(any("left the backup as it is" in line and "3 more day(s)" in line for line in output))

    def test_a_date_inside_the_last_day_of_a_longer_lock_relocks_it_to_the_end_of_that_day(self) -> None:
        asked = retention_till_text(datetime.now(timezone.utc) + timedelta(days=4, hours=10))
        calls, run = self._tool([SHORTENING_REFUSAL, None], info=self._info(5))

        result, warnings, output = self._apply(run, "01-vm.vmdk", retention_days=5, retention_till=asked)

        self.assertEqual([command[0] for command in calls], ["retention", "info", "retention"])
        self.assertEqual(_period(calls[0]), ["--retention-till", asked])
        self.assertEqual(_period(calls[2]), ["--retention", "5"])
        self.assertTrue(result["relocked"])
        self.assertGreaterEqual(datetime.fromisoformat(result["held_until"].replace("Z", "+00:00")), datetime.fromisoformat(asked.replace("Z", "+00:00")))
        self.assertEqual(warnings, [])
        self.assertTrue(any("held the backup for 5 day(s) from now" in line for line in output))

    def test_a_manifest_already_held_past_the_date_is_relocked_a_day_past_it(self) -> None:
        asked = retention_till_text(datetime.now(timezone.utc) + timedelta(days=4, hours=10))
        calls, run = self._tool([MANIFEST_REFUSAL, None])

        result, warnings, _output = self._apply(run, "manifest", retention_days=5, retention_till=asked)

        self.assertEqual([command[0] for command in calls], ["retention", "retention"])
        self.assertEqual(_period(calls[1]), ["--retention", "6"])
        self.assertTrue(result["relocked"])
        self.assertEqual(warnings, [])

    def test_compliance_is_never_relocked(self) -> None:
        asked = retention_till_text(datetime.now(timezone.utc) + timedelta(days=4, hours=10))
        for name, refusal, info in (("01-vm.vmdk", SHORTENING_REFUSAL, self._info(5, "compliance")), ("manifest", MANIFEST_REFUSAL, None)):
            with self.subTest(name=name):
                calls, run = self._tool([refusal, None], info=info)

                result, warnings, _output = self._apply(run, name, mode="compliance", retention_days=5, retention_till=asked)

                self.assertIsInstance(result, StorageToolError)
                self.assertEqual([command[0] for command in calls if command[0] == "retention"], ["retention"])
                self.assertEqual(len(warnings), 1)

    def test_compliance_proven_longer_is_left_as_it_is(self) -> None:
        calls, run = self._tool([SHORTENING_REFUSAL], info=self._info(9, "compliance"))

        result, warnings, _output = self._apply(run, "01-vm.vmdk", mode="compliance", retention_days=5)

        self.assertEqual([command[0] for command in calls], ["retention", "info"])
        self.assertFalse(result["relocked"])
        self.assertEqual(warnings, [])

    def test_a_lock_the_storage_does_not_report_is_a_failure(self) -> None:
        _calls, run = self._tool([SHORTENING_REFUSAL, None], info=self._info(9, "none"))

        result, warnings, _output = self._apply(run, "01-vm.vmdk", retention_days=5)

        self.assertIsInstance(result, StorageToolError)
        self.assertEqual(len(warnings), 1)

    def test_a_lock_shorter_than_asked_is_a_failure(self) -> None:
        calls, run = self._tool([SHORTENING_REFUSAL], info=self._info(1))

        result, warnings, _output = self._apply(run, "01-vm.vmdk", retention_days=3)

        self.assertIsInstance(result, StorageToolError)
        self.assertEqual([command[0] for command in calls], ["retention", "info"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("failed to update the retention", warnings[0])

    def test_a_relock_the_storage_refuses_too_is_a_failure(self) -> None:
        calls, run = self._tool([SHORTENING_REFUSAL, SHORTENING_REFUSAL], info=self._info(5))

        result, warnings, _output = self._apply(run, "01-vm.vmdk", retention_days=5)

        self.assertIsInstance(result, StorageToolError)
        self.assertEqual([command[0] for command in calls], ["retention", "info", "retention"])
        self.assertEqual(len(warnings), 1)

    def test_a_failure_that_is_not_a_refusal_does_not_read_the_lock(self) -> None:
        calls, run = self._tool(["Error: 'bucket' has no Object Lock configuration\n"])

        result, warnings, _output = self._apply(run, "01-vm.vmdk", retention_days=1)

        self.assertEqual([command[0] for command in calls], ["retention"])
        self.assertIsInstance(result, StorageToolError)
        self.assertEqual(len(warnings), 1)

    def test_a_successful_extension_is_unchanged(self) -> None:
        calls, run = self._tool([None])

        result, warnings, output = self._apply(run, "01-vm.vmdk", retention_days=5)

        self.assertEqual([command[0] for command in calls], ["retention"])
        self.assertEqual(result["status"], "ok")
        self.assertNotIn("held_longer", result)
        self.assertEqual(warnings, [])
        self.assertTrue(any(line.startswith("INFO") and "done" in line for line in output))

    def test_other_tool_failures_keep_their_warning(self) -> None:
        with (
            patch.object(storage_tool, "default_disk_tool_path", return_value=Path(__file__)),
            patch.object(
                storage_tool,
                "run_disk_tool_process",
                return_value=CompletedProcess(args=[], returncode=1, stdout="", stderr="Error: putObjectRetention: 'k': NoSuchKey: x"),
            ),
            self.assertLogs("python.srv.core.storage_tool", level="WARNING") as captured,
            self.assertRaises(StorageToolError),
        ):
            storage_tool.run_disk_tool(["retention", "--name", "manifest", "--folder", "b", "--mode", "s3"])

        self.assertEqual(len(captured.output), 1)


class CatalogKeepsTheHeldDateTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, JOBS, REPOSITORIES):
            store.clear()
        self.addCleanup(lambda: [store.clear() for store in (BACKUPS, JOBS, REPOSITORIES)])
        REPOSITORIES["repo-1"] = dict(_repository(), s3=dict(_repository()["s3"], immutability={"mode": "governance", "retention_days": 1, "retention_scope": "job_retention"}))
        JOBS["job-1"] = {
            "id": "job-1",
            "repo_id": "repo-1",
            "retention_days": 1,
            "active_full_schedule": {"kind": "weekly", "enabled": True, "weekdays": ["Thu"], "time": "00:00", "timezone": "UTC"},
        }
        BACKUPS["full"] = {
            "id": "full",
            "job_id": "job-1",
            "vm_id": "vm-1",
            "repository_id": "repo-1",
            "segment_id": "segment-1",
            "status": "success",
            "full_backup": True,
            "created_at": "2026-10-19T23:56:27Z",
            "immutability_expires_at": "2026-10-25T00:00:00+00:00",
            "artifact": {"disks": [{"abr_path": "lab/vm-1/full", "abr_backup_name": "vm-1-disk-0"}]},
        }

    def test_apply_immutability_never_asks_for_less_than_the_catalog_holds(self) -> None:
        with (
            patch.object(abr_v41, "_now_utc", return_value=datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)),
            patch.object(abr_v41, "disk_tool_supports_retention_till", return_value=True),
            patch.object(abr_v41, "apply_repository_immutability", return_value={"status": "ok"}) as apply,
        ):
            result = abr_v41.apply_immutability_to_existing_backups("repo-1")

        self.assertEqual(result["status"], "success")
        self.assertEqual({call.kwargs["retention_till"] for call in apply.call_args_list}, {"2026-10-25T00:00:00Z"})
        self.assertEqual(BACKUPS["full"]["immutability_expires_at"], "2026-10-25T00:00:00+00:00")

    def test_a_relocked_point_records_how_long_it_is_really_held(self) -> None:
        relocked = {"status": "ok", "relocked": True, "held_until": "2026-10-27T13:00:00Z"}
        with patch.object(abr_v41, "apply_repository_immutability", return_value=relocked):
            report = abr_v41.extend_segment_object_lock("segment-1", expires_at="2026-10-26T00:00:00+00:00")

        self.assertEqual(report["extended_restore_points"], ["full"])
        self.assertEqual(BACKUPS["full"]["immutability_expires_at"], "2026-10-27T13:00:00+00:00")

    def test_a_point_is_recorded_as_held_until_its_least_held_object(self) -> None:
        BACKUPS["full"]["artifact"]["disks"].append({"abr_path": "lab/vm-1/full", "abr_backup_name": "vm-1-disk-1"})
        results = [
            {"status": "ok", "relocked": True, "held_until": "2026-10-27T13:00:00Z"},
            {"status": "ok", "relocked": True, "held_until": "2026-10-26T13:00:00Z"},
            {"status": "ok", "relocked": True, "held_until": "2026-10-28T13:00:00Z"},
        ]
        with patch.object(abr_v41, "apply_repository_immutability", side_effect=results):
            abr_v41.extend_segment_object_lock("segment-1", expires_at="2026-10-26T00:00:00+00:00")

        self.assertEqual(BACKUPS["full"]["immutability_expires_at"], "2026-10-26T13:00:00+00:00")

    def test_a_point_extended_as_asked_records_the_date_asked(self) -> None:
        BACKUPS["full"]["artifact"]["disks"].append({"abr_path": "lab/vm-1/full", "abr_backup_name": "vm-1-disk-1"})
        results = [
            {"status": "ok", "held_until": "2026-10-26T00:00:00Z"},
            {"status": "ok", "relocked": True, "held_until": "2026-10-27T13:00:00Z"},
            {"status": "ok", "held_until": "2026-10-26T00:00:00Z"},
        ]
        with patch.object(abr_v41, "apply_repository_immutability", side_effect=results):
            abr_v41.extend_segment_object_lock("segment-1", expires_at="2026-10-26T00:00:00+00:00")

        self.assertEqual(BACKUPS["full"]["immutability_expires_at"], "2026-10-26T00:00:00+00:00")

    def test_a_point_left_as_it_is_keeps_the_date_asked(self) -> None:
        kept = {"status": "ok", "held_longer": True, "relocked": False, "held_until": "2026-10-26T00:00:00+00:00"}
        with patch.object(abr_v41, "apply_repository_immutability", return_value=kept):
            abr_v41.extend_segment_object_lock("segment-1", expires_at="2026-10-26T00:00:00+00:00")

        self.assertEqual(BACKUPS["full"]["immutability_expires_at"], "2026-10-26T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
