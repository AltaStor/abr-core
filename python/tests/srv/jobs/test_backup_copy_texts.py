# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import errno
import json
from pathlib import Path
from unittest.mock import patch

from python.srv.core import storage_tool
from python.srv.core.mongo_db_wrapper import JOBS, REPOSITORIES
from python.srv.core.storage_tool import StorageToolError
from python.srv.services import abr_v41, backup_copy, synthetic_full
from python.srv.services.backup_copy import BackupCopyError
from python.tests.srv.jobs.test_backup_copy import _Catalog

KEY = "et-test-copy-key-0123456789abcdef"


class FullScheduleRefusalTests(_Catalog):
    def test_a_backup_copy_job_without_a_full_schedule_is_named_as_one(self) -> None:
        error = self._refused(lambda: self._copy_job(active_full_schedule=None))

        self.assertIn("A backup copy job requires at least one full backup schedule", error.message)

    def test_a_backup_job_still_says_backup_job(self) -> None:
        error = self._refused(lambda: abr_v41._ensure_full_backup_schedule({}))

        self.assertIn("A backup job requires at least one full backup schedule", error.message)


class MissingSourceNamedTests(_Catalog):
    def test_each_kind_of_missing_source_is_named(self) -> None:
        cases = (
            ({"source_jobs": ["et-missing"]}, "Job not found: et-missing"),
            ({"source_jobs": ["Nightly", "et-missing"]}, "Job not found: et-missing"),
            ({"source_jobs": [], "source_job_ids": ["no-such-id"]}, "Job not found: no-such-id"),
            ({"source_jobs": [], "source_repositories": ["et-missing-repo"]}, "Repository not found: et-missing-repo"),
            ({"run_after_job": "et-missing"}, "Job not found: et-missing"),
        )
        for payload, message in cases:
            with self.subTest(payload=payload):
                error = self._refused(lambda payload=payload: self._copy_job(**payload))

                self.assertEqual((error.status_code, error.message), (404, message))

    def test_an_existing_source_is_still_taken(self) -> None:
        self.assertEqual(self._copy_job()["backup_copy"]["source_jobs"][0]["name"], "Nightly")


class AnnouncedTypeTests(_Catalog):
    def setUp(self) -> None:
        super().setUp()
        self._point("f1", "ActiveFull")
        self.copy = self._copy_job()
        self._copied("c1", "f1")

    def first_line(self) -> tuple[str, dict]:
        task = backup_copy.create_copy_sessions(JOBS[self.copy["id"]], trigger="manual")[0]
        line = next(entry["message"] for entry in task["logs"] if "backup copy of" in entry["message"])
        return line, task["result"]

    def test_a_copy_after_a_new_source_chain_is_announced_as_a_full(self) -> None:
        self._point("f2", "ActiveFull")

        line, result = self.first_line()

        self.assertTrue(line.endswith("(ActiveFull, SourceNewChain)"), line)
        self.assertEqual((result["resolved_backup_type"], result["resolved_backup_reason"]), ("ActiveFull", "SourceNewChain"))

    def test_a_copy_of_new_increments_is_still_announced_as_an_incremental(self) -> None:
        self._point("i1", "Incremental", parent_point_id="f1")

        line, result = self.first_line()

        self.assertIn("(Incremental, ", line)
        self.assertEqual(result["resolved_backup_type"], "Incremental")

    def test_the_announced_phase_rule(self) -> None:
        self.assertEqual(backup_copy._announced_phase("Incremental", "Manual", {"kind": "full", "damaged": False}), ("ActiveFull", "SourceNewChain"))
        self.assertEqual(backup_copy._announced_phase("Incremental", "Manual", {"kind": "full", "damaged": True}), ("ActiveFull", "DamagedChain"))
        self.assertEqual(backup_copy._announced_phase("Incremental", "Manual", {"kind": "incremental"}), ("Incremental", "Manual"))
        self.assertEqual(backup_copy._announced_phase("Incremental", "Manual", None), ("Incremental", "Manual"))
        self.assertEqual(backup_copy._announced_phase("SyntheticFull", "Schedule", {"kind": "full"}), ("SyntheticFull", "Schedule"))


PLAIN_MANIFEST = {"format_version": 1, "vm_config": {}, "encryption_enabled": False, "encryption_algorithm": 0}


class _CopyRun(_Catalog):

    def start(self, **job) -> dict:
        self._point("f1", "ActiveFull", size_bytes=900 * 1024 * 1024)
        self.copy = self._copy_job(retry_count=0, **job)
        task = backup_copy.create_copy_sessions(JOBS[self.copy["id"]], trigger="manual", include_idle=True)[0]
        self.enterContext(patch.object(backup_copy, "_source_manifest", return_value=dict(PLAIN_MANIFEST)))
        self.enterContext(patch("python.srv.services.synthetic_full.run_disk_tool"))
        self.enterContext(patch("python.srv.services.synthetic_full.apply_repository_immutability"))
        self.enterContext(patch("python.srv.services.synthetic_full.remove_disk_tool_backup"))
        return task


class CopySpaceMessageTests(_CopyRun):
    def test_a_copy_that_runs_out_of_room_speaks_of_the_copy(self) -> None:
        task = self.start()
        with (
            patch.object(backup_copy, "copy_disk_tool_chain", side_effect=StorageToolError("write failed")),
            patch.object(backup_copy, "_free_bytes_at_start", return_value=300 * 1024 * 1024),
            self.assertRaises(BackupCopyError) as caught,
        ):
            backup_copy.run_backup_copy(task)

        message = str(caught.exception)
        self.assertIn("this copy needs about 900.0 MiB", message)
        self.assertIn("when the copy started", message)
        self.assertIn("or copy into another repository", message)
        self.assertNotIn("merge", message)
        self.assertNotIn("synthetic", message)

    def test_a_full_disk_while_copying_speaks_of_the_copy(self) -> None:
        task = self.start()
        with (
            patch.object(backup_copy, "copy_disk_tool_chain", side_effect=OSError(errno.ENOSPC, "No space left on device")),
            self.assertRaises(BackupCopyError) as caught,
        ):
            backup_copy.run_backup_copy(task)

        self.assertIn("or copy into another repository", str(caught.exception))

    def test_a_synthetic_full_keeps_its_own_words(self) -> None:
        message = synthetic_full._no_room_detail(REPOSITORIES["dst"], [{"size_bytes": 900 * 1024 * 1024}], 300 * 1024 * 1024)

        self.assertIn("this synthetic full needs about", message)
        self.assertIn("or merge into another repository", message)



class CopyToolEndTests(_CopyRun):
    def failure(self, text: str) -> str:
        task = self.start()
        with (
            patch.object(backup_copy, "copy_disk_tool_chain", side_effect=StorageToolError(text)),
            patch.object(backup_copy, "_free_bytes_at_start", return_value=None),
            self.assertRaises(BackupCopyError) as caught,
        ):
            backup_copy.run_backup_copy(task)
        return str(caught.exception)

    def test_a_killed_tool_is_named_as_the_end_of_the_copy(self) -> None:
        text = storage_tool.disk_tool_failure_text(-9, "", "")

        message = self.failure(text)

        self.assertTrue(message.startswith("Copying disk 1/1 failed: the copy of the restore point ended unexpectedly: abr-vmdk-tool was terminated by signal 9 (SIGKILL)"))
        self.assertIn(storage_tool.OUT_OF_MEMORY_HINT, message)
        self.assertNotIn("backup tool", message)

    def test_an_interrupted_tool_speaks_of_the_copy_not_a_disk_or_a_vm(self) -> None:
        message = self.failure(storage_tool.disk_tool_failure_text(1, "Interrupted, releasing the disk and the VM\n", ""))

        self.assertEqual(
            message,
            "Copying disk 1/1 failed: abr-vmdk-tool was interrupted by a signal from outside the server (SIGTERM or SIGINT) "
            "and stopped copying the restore point; what it had written is discarded",
        )

    def test_an_error_the_tool_describes_is_passed_on_as_it_is(self) -> None:
        self.assertEqual(self.failure("cannot open the source backup"), "Copying disk 1/1 failed: cannot open the source backup")

    def test_a_backup_keeps_its_own_words(self) -> None:
        self.assertTrue(storage_tool.disk_tool_failure_text(-9, "", "").startswith("the backup tool ended unexpectedly"))


class CopyManifestAlgorithmTests(_CopyRun):
    def run_copy(self, **job) -> tuple[dict, dict]:
        task = self.start(**job)
        with patch.object(backup_copy, "copy_disk_tool_chain", return_value={"raw_data_size": 10, "stored_data_size": 4, "merged_from": 1}):
            result = backup_copy.run_backup_copy(task)
        return json.loads(Path(result["manifest_path"]).read_text()), result

    def test_an_encrypted_copy_of_a_plain_source_records_its_algorithm(self) -> None:
        manifest, result = self.run_copy(task_parameters={"encryption_enabled": True, "encryption_key": KEY})

        self.assertEqual((manifest["encryption_enabled"], manifest["encryption_algorithm"]), (True, 1))
        self.assertEqual((result["encryption_enabled"], result["encryption_algorithm"]), (True, 1))

    def test_a_plain_copy_records_none(self) -> None:
        manifest, result = self.run_copy()

        self.assertEqual((manifest["encryption_enabled"], manifest["encryption_algorithm"]), (False, 0))
        self.assertEqual(result["encryption_algorithm"], 0)

