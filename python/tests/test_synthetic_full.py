# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import BACKUPS, JOBS, REPOSITORIES, TASKS
from python.srv.core.storage_tool import StorageToolError
from python.srv.core.subprocess_util import ProcessCancelled
from python.srv.core.task_executor import TaskStopRequested
from python.srv.services import abr_v41, synthetic_full
from python.srv.services.synthetic_full import (
    SyntheticFullError,
    SyntheticFullValidationError,
    build_disk_merges,
    is_synthetic_full_task,
    validate_chain,
)

REPO_ID = "repo-1"
JOB_ID = "job-1"
VM_ID = "vm-1"


def _disk(task_id: str, name: str = "01-disk.vmdk", disk_key: str = "2000") -> dict[str, object]:
    return {
        "index": 0,
        "disk_key": disk_key,
        "disk_label": "Hard disk 1",
        "abr_path": f"lab/web01/{task_id}",
        "abr_backup_name": name,
        "source_disk_type_code": 5,
        "source_disk_type_name": "thin",
    }


def _point(point_id: str, execution_type: str, created_at: str, disks: list[dict[str, object]]) -> dict[str, object]:
    return {
        "id": point_id,
        "job_id": JOB_ID,
        "vm_id": VM_ID,
        "vm_name": "web01",
        "repository_id": REPO_ID,
        "status": "success",
        "execution_type": execution_type,
        "created_at": created_at,
        "artifact": {"disks": disks},
    }


def _chain() -> list[dict[str, object]]:
    return [
        _point("p1", "ActiveFull", "2026-01-01T00:00:00Z", [_disk("task-a")]),
        _point("p2", "Incremental", "2026-01-02T00:00:00Z", [_disk("task-b")]),
        _point("p3", "Incremental", "2026-01-03T00:00:00Z", [_disk("task-c")]),
    ]


class SyntheticFullDetectionTests(unittest.TestCase):
    def test_task_is_recognised_by_flag_or_resolved_type(self) -> None:
        self.assertTrue(is_synthetic_full_task({"parameters": {"synthetic_full": True}}))
        self.assertTrue(is_synthetic_full_task({"parameters": {"resolved_backup_type": "SyntheticFull"}}))
        self.assertFalse(is_synthetic_full_task({"parameters": {"resolved_backup_type": "ActiveFull"}}))
        self.assertFalse(is_synthetic_full_task({}))


class SyntheticFullValidationTests(unittest.TestCase):
    def test_valid_chain_passes(self) -> None:
        validate_chain(_chain())

    def test_empty_chain_is_rejected(self) -> None:
        with self.assertRaises(SyntheticFullValidationError) as error:
            validate_chain([])
        self.assertIn("run a full backup first", str(error.exception))

    def test_chain_not_starting_with_a_full_is_rejected(self) -> None:
        chain = _chain()
        chain[0]["execution_type"] = "Incremental"
        with self.assertRaises(SyntheticFullValidationError) as error:
            validate_chain(chain)
        self.assertIn("start with a full backup", str(error.exception))

    def test_full_in_the_middle_is_rejected(self) -> None:
        chain = _chain()
        chain[2]["execution_type"] = "ActiveFull"
        with self.assertRaises(SyntheticFullValidationError) as error:
            validate_chain(chain)
        self.assertIn("must be incremental", str(error.exception))

    def test_chain_without_incrementals_is_rejected(self) -> None:
        with self.assertRaises(SyntheticFullValidationError) as error:
            validate_chain(_chain()[:1])
        self.assertIn("no incrementals", str(error.exception))

    def test_chain_describing_different_disks_is_rejected(self) -> None:
        chain = _chain()
        chain[1]["artifact"]["disks"] = [_disk("task-b", name="09-other.vmdk")]
        with self.assertRaises(SyntheticFullValidationError) as error:
            validate_chain(chain)
        self.assertIn("different disks", str(error.exception))

    def test_point_without_artifacts_is_rejected(self) -> None:
        chain = _chain()
        chain[1]["artifact"] = {"disks": []}
        with self.assertRaises(SyntheticFullValidationError) as error:
            validate_chain(chain)
        self.assertIn("no backup artifacts", str(error.exception))


class SourceStorageArgsTests(unittest.TestCase):
    def test_a_local_source_never_inherits_the_destination_path(self) -> None:
        from python.srv.core.storage_tool import disk_tool_source_storage_args

        args = disk_tool_source_storage_args({"type": "local", "path": "/srv/backups"}, abr_path="/srv/backups/web01")

        self.assertIn("--source-path", args)
        self.assertEqual(args[args.index("--source-path") + 1], "")

    def test_an_s3_source_keeps_its_own_path(self) -> None:
        from python.srv.core.storage_tool import disk_tool_source_storage_args

        repository = {"type": "s3", "s3": {"bucket": "b", "prefix": "lab", "region": "us-east-1"}}
        args = disk_tool_source_storage_args(repository, abr_path="lab/web01")

        self.assertEqual(args[args.index("--source-path") + 1], "lab/web01")
        self.assertEqual(args.count("--source-path"), 1)


class SyntheticFullDiskMergeTests(unittest.TestCase):
    def test_names_are_qualified_against_the_common_source_path(self) -> None:
        merges = build_disk_merges(_chain())

        self.assertEqual(len(merges), 1)
        self.assertEqual(merges[0]["source_path"], "lab/web01")
        self.assertEqual(
            merges[0]["source_names"],
            ["task-a/01-disk.vmdk", "task-b/01-disk.vmdk", "task-c/01-disk.vmdk"],
        )

    def test_each_disk_gets_its_own_chain(self) -> None:
        chain = [
            _point("p1", "ActiveFull", "2026-01-01T00:00:00Z",
                   [_disk("task-a", "01-disk.vmdk", "2000"), _disk("task-a", "02-disk.vmdk", "2001")]),
            _point("p2", "Incremental", "2026-01-02T00:00:00Z",
                   [_disk("task-b", "01-disk.vmdk", "2000"), _disk("task-b", "02-disk.vmdk", "2001")]),
        ]

        merges = {merge["identity"]: merge for merge in build_disk_merges(chain)}

        self.assertEqual(len(merges), 2)
        self.assertEqual(
            merges["abr_backup_name:01-disk.vmdk"]["source_names"], ["task-a/01-disk.vmdk", "task-b/01-disk.vmdk"]
        )
        self.assertEqual(
            merges["abr_backup_name:02-disk.vmdk"]["source_names"], ["task-a/02-disk.vmdk", "task-b/02-disk.vmdk"]
        )

    def test_a_full_without_a_cbt_disk_key_still_pairs_with_its_incremental(self) -> None:
        full_disk = {**_disk("task-a"), "disk_key": None}
        incremental_disk = {**_disk("task-b"), "disk_key": "2000"}
        chain = [
            _point("p1", "ActiveFull", "2026-01-01T00:00:00Z", [full_disk]),
            _point("p2", "Incremental", "2026-01-02T00:00:00Z", [incremental_disk]),
        ]

        validate_chain(chain)
        merges = build_disk_merges(chain)

        self.assertEqual(len(merges), 1)
        self.assertEqual(merges[0]["source_names"], ["task-a/01-disk.vmdk", "task-b/01-disk.vmdk"])

    def test_local_repository_paths_keep_their_absolute_root(self) -> None:
        chain = [
            _point("p1", "ActiveFull", "2026-01-01T00:00:00Z", [{**_disk("task-a"), "abr_path": "/srv/backups/web01/task-a"}]),
            _point("p2", "Incremental", "2026-01-02T00:00:00Z", [{**_disk("task-b"), "abr_path": "/srv/backups/web01/task-b"}]),
        ]

        merges = build_disk_merges(chain)

        self.assertEqual(merges[0]["source_path"], "/srv/backups/web01")
        self.assertEqual(merges[0]["source_names"], ["task-a/01-disk.vmdk", "task-b/01-disk.vmdk"])

    def test_missing_storage_location_is_rejected(self) -> None:
        chain = _chain()
        chain[1]["artifact"]["disks"][0]["abr_path"] = ""
        with self.assertRaises(SyntheticFullValidationError) as error:
            build_disk_merges(chain)
        self.assertIn("missing stored backup locations", str(error.exception))


class SyntheticFullRunTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, JOBS, REPOSITORIES, TASKS):
            store.clear()
        self._root = tempfile.mkdtemp(prefix="abr-synthetic-test-")
        self.addCleanup(shutil.rmtree, self._root, ignore_errors=True)
        self._manifest_patch = patch(
            "python.srv.services.synthetic_full._source_manifest",
            return_value={"format_version": 1, "vm_config": {"firmware": "efi"}},
        )
        self._manifest_patch.start()
        self.addCleanup(self._manifest_patch.stop)
        self._tool_patch = patch("python.srv.services.synthetic_full.run_disk_tool")
        self._tool_patch.start()
        self.addCleanup(self._tool_patch.stop)
        REPOSITORIES[REPO_ID] = {"id": REPO_ID, "name": "Local", "type": "local", "path": self._root}
        JOBS[JOB_ID] = {"id": JOB_ID, "name": "Nightly", "job_type": "backup", "repo_id": REPO_ID, "retention_days": 14}
        for point in _chain():
            BACKUPS[str(point["id"])] = point

    def tearDown(self) -> None:
        for store in (BACKUPS, JOBS, REPOSITORIES, TASKS):
            store.clear()

    def _task(self) -> dict[str, object]:
        return {
            "id": "task-new",
            "job_id": JOB_ID,
            "task_type": "backup",
            "resource_type": "virtual_machine",
            "resource_external_id": VM_ID,
            "status": "queued",
            "parameters": {"repository_id": REPO_ID, "synthetic_full": True,
                           "resolved_backup_type": "SyntheticFull"},
        }

    def test_a_merge_waiting_on_an_increment_refuses_when_it_did_not_complete(self) -> None:
        from python.srv.core.mongo_db_wrapper import TASKS

        task = self._task()
        task["parameters"]["requires_task_id"] = "task-increment"
        self.addCleanup(TASKS.pop, "task-increment", None)

        for status in ("failed", "stopped", "running"):
            TASKS["task-increment"] = {"id": "task-increment", "status": status}
            with self.assertRaises(synthetic_full.SyntheticFullValidationError) as ctx:
                synthetic_full.run_synthetic_full(task)
            self.assertIn(status, str(ctx.exception))

    def test_a_merge_waiting_on_a_vanished_increment_refuses(self) -> None:
        from python.srv.core.mongo_db_wrapper import TASKS

        TASKS.pop("task-increment", None)
        task = self._task()
        task["parameters"]["requires_task_id"] = "task-increment"

        with self.assertRaises(synthetic_full.SyntheticFullValidationError):
            synthetic_full.run_synthetic_full(task)

    @patch("python.srv.services.synthetic_full.copy_disk_tool_chain")
    def test_a_merge_runs_once_its_increment_has_completed(self, copy_mock) -> None:
        from python.srv.core.mongo_db_wrapper import TASKS

        copy_mock.return_value = {"raw_data_size": 1024, "stored_data_size": 512, "merged_from": 3}
        TASKS["task-increment"] = {"id": "task-increment", "status": "completed"}
        self.addCleanup(TASKS.pop, "task-increment", None)
        task = self._task()
        task["parameters"]["requires_task_id"] = "task-increment"

        result = synthetic_full.run_synthetic_full(task)

        self.assertTrue(result["synthetic_full"])

    @patch("python.srv.services.synthetic_full.copy_disk_tool_chain")
    def test_merge_produces_a_synthetic_full_result(self, copy_mock) -> None:
        copy_mock.return_value = {"raw_data_size": 1024, "stored_data_size": 512, "merged_from": 3}
        updates: list[dict[str, object]] = []

        result = synthetic_full.run_synthetic_full(self._task(), progress_callback=updates.append)

        self.assertEqual(result["resolved_backup_type"], "SyntheticFull")
        self.assertTrue(result["synthetic_full"])
        self.assertEqual(result["merged_from"], 3)
        self.assertEqual(result["merged_restore_point_ids"], ["p1", "p2", "p3"])
        self.assertEqual(result["disk_count"], 1)
        self.assertEqual(result["disks"][0]["abr_backup_name"], "01-disk.vmdk")
        self.assertEqual(result["disks"][0]["source_disk_type_code"], 5)
        self.assertEqual(result["disks"][0]["disk_label"], "Hard disk 1")
        self.assertEqual(result["worker_mode"], "synthetic-full")
        self.assertEqual(updates[-1]["progress"], 100)
        manifest = json.loads(open(result["manifest_path"], encoding="utf-8").read())
        self.assertEqual(manifest["resolved_backup_type"], "SyntheticFull")
        self.assertFalse(manifest["incremental"])
        self.assertEqual(manifest["merged_restore_point_ids"], ["p1", "p2", "p3"])
        self.assertEqual(manifest["vm_config"], {"firmware": "efi"})

    @patch("python.srv.services.synthetic_full.copy_disk_tool_chain")
    def test_a_local_merge_falls_back_to_the_written_size(self, copy_mock) -> None:
        def _write(**kwargs: object) -> dict[str, object]:
            destination = Path(self._root) / "web01" / "task-new"
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "01-disk.vmdk.abrd").write_bytes(b"x" * 4096)
            return {"raw_data_size": 0, "stored_data_size": 0, "merged_from": 3}

        copy_mock.side_effect = _write

        result = synthetic_full.run_synthetic_full(self._task())

        self.assertGreaterEqual(result["transferred_bytes"], 4096)

    @patch("python.srv.services.synthetic_full.copy_disk_tool_chain")
    def test_a_target_repository_receives_the_merge_while_the_chain_is_read_from_its_own(self, copy_mock) -> None:
        copy_mock.return_value = {"raw_data_size": 1, "stored_data_size": 1, "merged_from": 3}
        target = tempfile.mkdtemp(prefix="abr-synthetic-target-")
        self.addCleanup(shutil.rmtree, target, ignore_errors=True)
        REPOSITORIES["repo-2"] = {"id": "repo-2", "name": "Archive", "type": "local", "path": target}
        task = self._task()
        task["parameters"]["repository_id"] = "repo-2"
        task["parameters"]["source_repository_id"] = REPO_ID

        result = synthetic_full.run_synthetic_full(task)

        kwargs = copy_mock.call_args.kwargs
        self.assertEqual(kwargs["source_repository"]["id"], REPO_ID)
        self.assertEqual(kwargs["destination_repository"]["id"], "repo-2")
        self.assertEqual(result["repository_id"], "repo-2")
        self.assertEqual(result["source_repository_id"], REPO_ID)
        self.assertTrue(result["backup_root"].startswith(target))

    @patch("python.srv.services.synthetic_full.copy_disk_tool_chain")
    def test_chain_is_read_from_its_own_location(self, copy_mock) -> None:
        copy_mock.return_value = {"raw_data_size": 1, "stored_data_size": 1, "merged_from": 3}

        synthetic_full.run_synthetic_full(self._task())

        kwargs = copy_mock.call_args.kwargs
        self.assertEqual(kwargs["source_abr_path"], "lab/web01")
        self.assertEqual(kwargs["source_names"], ["task-a/01-disk.vmdk", "task-b/01-disk.vmdk", "task-c/01-disk.vmdk"])
        self.assertNotEqual(kwargs["destination_abr_path"], kwargs["source_abr_path"])

    @patch("python.srv.services.synthetic_full.copy_disk_tool_chain")
    def test_invalid_chain_is_rejected_before_anything_is_written(self, copy_mock) -> None:
        BACKUPS["p3"]["artifact"]["disks"] = [_disk("task-c", name="09-other.vmdk")]

        with self.assertRaises(SyntheticFullValidationError):
            synthetic_full.run_synthetic_full(self._task())

        copy_mock.assert_not_called()

    @patch("python.srv.services.synthetic_full.copy_disk_tool_chain")
    def test_a_newer_full_starts_a_fresh_chain_to_merge(self, copy_mock) -> None:
        copy_mock.return_value = {"raw_data_size": 1, "stored_data_size": 1, "merged_from": 2}
        BACKUPS["p2"]["execution_type"] = "ActiveFull"

        result = synthetic_full.run_synthetic_full(self._task())

        self.assertEqual(result["merged_restore_point_ids"], ["p2", "p3"])

    @patch("python.srv.services.synthetic_full.remove_disk_tool_backup")
    @patch("python.srv.services.synthetic_full.copy_disk_tool_chain")
    def test_a_failure_part_way_through_discards_the_disks_already_merged(self, copy_mock, remove_mock) -> None:
        for point_id in ("p1", "p2", "p3"):
            BACKUPS[point_id]["artifact"]["disks"] = [_disk("task-x", "01-disk.vmdk"), _disk("task-x", "02-disk.vmdk")]
        copy_mock.side_effect = [
            {"raw_data_size": 1, "stored_data_size": 1, "merged_from": 3},
            StorageToolError("disk full"),
        ]

        with self.assertRaises(SyntheticFullError):
            synthetic_full.run_synthetic_full(self._task())

        removed = [call.args[2] for call in remove_mock.call_args_list]
        self.assertEqual(removed, ["01-disk.vmdk"])

    @patch("python.srv.services.synthetic_full.remove_disk_tool_backup")
    @patch("python.srv.services.synthetic_full.copy_disk_tool_chain")
    def test_an_object_lock_that_blocks_cleanup_is_reported(self, copy_mock, remove_mock) -> None:
        for point_id in ("p1", "p2", "p3"):
            BACKUPS[point_id]["artifact"]["disks"] = [_disk("task-x", "01-disk.vmdk"), _disk("task-x", "02-disk.vmdk")]
        copy_mock.side_effect = [
            {"raw_data_size": 1, "stored_data_size": 1, "merged_from": 3},
            StorageToolError("disk full"),
        ]
        remove_mock.side_effect = StorageToolError("WORM protected")

        with self.assertLogs("python.srv.services.synthetic_full", level="ERROR") as logs:
            with self.assertRaises(SyntheticFullError):
                synthetic_full.run_synthetic_full(self._task())

        self.assertTrue(any("left 1 merged disk(s) behind" in line for line in logs.output))

    @patch("python.srv.services.synthetic_full.remove_disk_tool_backup")
    @patch("python.srv.services.synthetic_full.copy_disk_tool_chain")
    def test_cancellation_discards_what_was_already_merged(self, copy_mock, remove_mock) -> None:
        for point_id in ("p1", "p2", "p3"):
            BACKUPS[point_id]["artifact"]["disks"] = [_disk("task-x", "01-disk.vmdk"), _disk("task-x", "02-disk.vmdk")]
        copy_mock.side_effect = [
            {"raw_data_size": 1, "stored_data_size": 1, "merged_from": 3},
            ProcessCancelled("cancellation requested"),
        ]

        with self.assertRaises(TaskStopRequested):
            synthetic_full.run_synthetic_full(self._task())

        self.assertEqual([call.args[2] for call in remove_mock.call_args_list], ["01-disk.vmdk"])

    @patch("python.srv.services.synthetic_full.copy_disk_tool_chain")
    def test_cancellation_is_surfaced_as_a_stop_request(self, copy_mock) -> None:
        copy_mock.side_effect = ProcessCancelled("cancellation requested")

        with self.assertRaises(TaskStopRequested) as error:
            synthetic_full.run_synthetic_full(self._task())

        self.assertIn("discarded", str(error.exception))

    @patch("python.srv.services.synthetic_full.copy_disk_tool_chain")
    def test_storage_failure_is_reported_as_a_runtime_error(self, copy_mock) -> None:
        from python.srv.core.storage_tool import StorageToolError

        copy_mock.side_effect = StorageToolError("disk full")

        with self.assertRaises(SyntheticFullError) as error:
            synthetic_full.run_synthetic_full(self._task())

        self.assertNotIsInstance(error.exception, SyntheticFullValidationError)
        self.assertIn("disk full", str(error.exception))

    @patch("python.srv.services.synthetic_full.apply_repository_immutability")
    @patch("python.srv.services.synthetic_full.copy_disk_tool_chain")
    def test_the_manifest_is_locked_for_the_same_period_as_the_data(self, copy_mock, lock_mock) -> None:
        copy_mock.return_value = {"raw_data_size": 1, "stored_data_size": 1, "merged_from": 3}
        REPOSITORIES[REPO_ID] = {
            "id": REPO_ID, "name": "S3", "type": "s3",
            "metadata": {"s3": {"bucket": "b", "region": "us-east-1",
                                "immutability": {"mode": "governance", "retention_days": 30}}},
        }

        synthetic_full.run_synthetic_full(self._task())

        self.assertEqual(copy_mock.call_args.kwargs["retention_days"], 30)
        self.assertEqual(lock_mock.call_args.kwargs["retention_days"], 30)

    @patch("python.srv.services.synthetic_full.apply_repository_immutability")
    @patch("python.srv.services.synthetic_full.copy_disk_tool_chain")
    def test_a_synthetic_full_opens_a_block_generation_of_its_own(self, copy_mock, lock_mock) -> None:
        copy_mock.return_value = {"raw_data_size": 1, "stored_data_size": 1, "merged_from": 3}
        REPOSITORIES[REPO_ID] = {
            "id": REPO_ID, "name": "S3", "type": "s3",
            "metadata": {"s3": {"bucket": "b", "region": "us-east-1",
                                "immutability": {"mode": "governance", "retention_days": 30}}},
        }

        result = synthetic_full.run_synthetic_full(self._task())

        generation = result["immutability_generation"]
        self.assertTrue(generation["starts_new_generation"])
        self.assertEqual(generation["lock_days"], 30)
        self.assertTrue(generation["expires_at"])

    @patch("python.srv.services.synthetic_full.apply_repository_immutability")
    @patch("python.srv.services.synthetic_full.copy_disk_tool_chain")
    def test_a_synthetic_full_is_locked_for_the_job_retention_when_that_is_longer(self, copy_mock, lock_mock) -> None:
        copy_mock.return_value = {"raw_data_size": 1, "stored_data_size": 1, "merged_from": 3}
        REPOSITORIES[REPO_ID] = {
            "id": REPO_ID, "name": "S3", "type": "s3",
            "metadata": {"s3": {"bucket": "b", "region": "us-east-1",
                                "immutability": {"mode": "governance", "retention_days": 5}}},
        }

        synthetic_full.run_synthetic_full(self._task())

        self.assertEqual(copy_mock.call_args.kwargs["retention_days"], 24)
        self.assertEqual(lock_mock.call_args.kwargs["retention_days"], 24)

    @patch("python.srv.services.synthetic_full.apply_repository_immutability")
    @patch("python.srv.services.synthetic_full.copy_disk_tool_chain")
    def test_a_synthetic_full_ignores_the_job_retention_under_minimum_scope(self, copy_mock, lock_mock) -> None:
        copy_mock.return_value = {"raw_data_size": 1, "stored_data_size": 1, "merged_from": 3}
        REPOSITORIES[REPO_ID] = {
            "id": REPO_ID, "name": "S3", "type": "s3",
            "metadata": {"s3": {"bucket": "b", "region": "us-east-1",
                                "immutability": {"mode": "governance", "retention_days": 5,
                                                 "retention_scope": "minimum_only"}}},
        }

        synthetic_full.run_synthetic_full(self._task())

        self.assertEqual(copy_mock.call_args.kwargs["retention_days"], 5)
        self.assertEqual(lock_mock.call_args.kwargs["retention_days"], 5)

    @patch("python.srv.services.synthetic_full.copy_disk_tool_chain")
    def test_an_unreadable_source_manifest_stops_the_merge(self, copy_mock) -> None:
        copy_mock.return_value = {"raw_data_size": 1, "stored_data_size": 1, "merged_from": 3}
        self._manifest_patch.stop()
        with patch("python.srv.services.synthetic_full._source_manifest", return_value={}):
            with self.assertRaises(SyntheticFullError) as error:
                synthetic_full.run_synthetic_full(self._task())
        self._manifest_patch.start()
        self.assertIn("would not be restorable", str(error.exception))

    def test_unsupported_repository_type_is_rejected(self) -> None:
        REPOSITORIES[REPO_ID] = {"id": REPO_ID, "name": "Share", "type": "smb", "path": "//host/share"}

        with self.assertRaises(SyntheticFullValidationError) as error:
            synthetic_full.run_synthetic_full(self._task())

        self.assertIn("cannot merge a chain", str(error.exception))


class SyntheticFullCliTests(unittest.TestCase):
    def _parse(self, argv: list[str]):
        from python.cli.parsing.parser_builder import build_parser

        return build_parser().parse_args(argv)

    def test_synthetic_flag_is_accepted(self) -> None:
        args = self._parse(["job", "start", "--name", "Nightly", "--synthetic"])
        self.assertTrue(args.synthetic)
        self.assertFalse(args.full)

    def test_full_and_synthetic_are_mutually_exclusive(self) -> None:
        from python.cli.parsing.parser_builder import build_parser

        with self.assertRaises(SystemExit):
            build_parser().parse_args(["job", "start", "--name", "N", "--full", "--synthetic"])

    def test_validation_failure_gets_its_own_exit_code(self) -> None:
        from python.cli.commands.job_commands import _session_failure_exit_code
        from python.cli.exit_codes import INVALID_STATE, VALIDATION_FAILED

        self.assertEqual(_session_failure_exit_code({"failure_kind": "validation"}), VALIDATION_FAILED)
        self.assertEqual(_session_failure_exit_code({"failure_kind": "runtime"}), INVALID_STATE)
        self.assertEqual(_session_failure_exit_code({}), INVALID_STATE)


class SyntheticFullJobStartTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, JOBS, REPOSITORIES, TASKS):
            store.clear()

    def tearDown(self) -> None:
        for store in (BACKUPS, JOBS, REPOSITORIES, TASKS):
            store.clear()

    def test_synthetic_run_resolves_to_a_synthetic_full(self) -> None:
        from python.srv.services.abr_v41 import resolve_job_execution

        REPOSITORIES[REPO_ID] = {"id": REPO_ID, "name": "Local", "type": "local", "path": "/srv/backups"}
        job = {"id": JOB_ID, "repo_id": REPO_ID}

        self.assertEqual(
            resolve_job_execution(job, vm_id=VM_ID, force_full=False, force_synthetic=True),
            ("SyntheticFull", "ForceSynthetic"),
        )

    def test_a_target_repository_needs_the_synthetic_flag(self) -> None:
        from python.srv.services.abr_v41 import start_job_record
        from python.srv.services.api_errors import ApiError

        REPOSITORIES[REPO_ID] = {"id": REPO_ID, "name": "Local", "type": "local", "path": "/srv/backups"}
        JOBS[JOB_ID] = {"id": JOB_ID, "name": "Nightly", "job_type": "backup", "repo_id": REPO_ID,
                        "vm_ids": [VM_ID], "vm_names": ["web01"]}

        with self.assertRaises(ApiError) as error:
            start_job_record(JOB_ID, target_repository_id=REPO_ID)
        self.assertIn("only used by a synthetic full", error.exception.message)

    def test_synthetic_run_is_refused_on_a_repository_that_cannot_merge(self) -> None:
        from python.srv.services.abr_v41 import resolve_job_execution
        from python.srv.services.api_errors import ApiError

        REPOSITORIES[REPO_ID] = {"id": REPO_ID, "name": "Share", "type": "smb", "path": "//host/share"}
        job = {"id": JOB_ID, "repo_id": REPO_ID, "repo_name": "Share"}

        with self.assertRaises(ApiError) as error:
            resolve_job_execution(job, vm_id=VM_ID, force_full=False, force_synthetic=True)
        self.assertIn("cannot merge a backup chain", error.exception.message)


class FullBackupParentTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, JOBS, REPOSITORIES, TASKS):
            store.clear()

    def tearDown(self) -> None:
        for store in (BACKUPS, JOBS, REPOSITORIES, TASKS):
            store.clear()

    def _register(self, execution_type: str, task_id: str) -> dict[str, object]:
        from python.srv.services.backup_catalog import register_backup_from_completed_task

        JOBS[JOB_ID] = {"id": JOB_ID, "name": "Nightly", "job_type": "backup", "repo_id": REPO_ID,
                        "repo_name": "Local", "vm_ids": [VM_ID], "vm_names": ["web01"]}
        REPOSITORIES[REPO_ID] = {"id": REPO_ID, "name": "Local", "type": "local", "path": "/srv/backups"}
        task = {
            "id": task_id,
            "job_id": JOB_ID,
            "task_type": "backup",
            "status": "completed",
            "resource_type": "virtual_machine",
            "resource_external_id": VM_ID,
            "updated_at": f"2026-01-0{task_id[-1]}T00:00:00Z",
            "parameters": {"repository_id": REPO_ID},
            "result": {
                "resolved_backup_type": execution_type,
                "repository_id": REPO_ID,
                "repository_type": "local",
                "finished_at": f"2026-01-0{task_id[-1]}T00:00:00Z",
                "disks": [{"index": 1, "abr_path": f"/srv/backups/web01/{task_id}",
                           "abr_backup_name": "01-disk.vmdk"}],
                "manifest_path": f"/srv/backups/web01/{task_id}/manifest.json",
            },
        }
        return register_backup_from_completed_task(task) or {}

    def test_a_full_backup_never_records_a_parent(self) -> None:
        self._register("ActiveFull", "task-1")
        self._register("Incremental", "task-2")
        synthetic = self._register("SyntheticFull", "task-3")
        second_full = self._register("ActiveFull", "task-4")

        self.assertIsNone(synthetic.get("parent_point_id"))
        self.assertIsNone(second_full.get("parent_point_id"))

    def test_a_completed_backup_session_names_the_restore_point_it_created(self) -> None:
        from concurrent.futures import Future

        from python.srv.core.task_pool import TaskProcessPool

        task = {"id": "task-1", "task_type": "backup", "status": "queued", "result": {}, "logs": []}
        TASKS["task-1"] = task
        self.addCleanup(TASKS.pop, "task-1", None)
        outcome = {
            "task_id": "task-1",
            "status": "completed",
            "result": {"operation": "backup"},
            "updated_at": "2026-09-14T00:00:00+00:00",
        }
        pool = TaskProcessPool(worker_count=1)

        future: Future = Future()
        future.set_result(outcome)
        pool._futures[future] = ("task-1", "backup", None)

        with patch(
            "python.srv.core.task_pool.register_backup_from_completed_task",
            return_value={"id": "point-9", "chain_id": "chain-9"},
        ):
            pool._complete_task(future)

        self.assertEqual(TASKS["task-1"]["result"]["restore_point_id"], "point-9")
        self.assertEqual(TASKS["task-1"]["result"]["backup_id"], "chain-9")

    def test_the_session_passes_the_created_restore_point_through(self) -> None:
        task = {
            "id": "task-1",
            "task_type": "backup",
            "status": "completed",
            "result": {"restore_point_id": "point-9", "backup_id": "chain-9"},
        }

        dto = abr_v41.session_to_dto(task)

        self.assertEqual(dto["restore_point_id"], "point-9")
        self.assertEqual(dto["backup_id"], "chain-9")

    def test_a_synthetic_full_session_reports_what_it_merged(self) -> None:
        REPOSITORIES["repo-src"] = {"id": "repo-src", "name": "Nightly", "type": "local"}
        REPOSITORIES["repo-dst"] = {"id": "repo-dst", "name": "Archive", "type": "s3"}
        self.addCleanup(REPOSITORIES.pop, "repo-src", None)
        self.addCleanup(REPOSITORIES.pop, "repo-dst", None)
        task = {
            "id": "task-1",
            "task_type": "backup",
            "status": "completed",
            "result": {
                "synthetic_full": True,
                "resolved_backup_type": "SyntheticFull",
                "merged_from": 3,
                "merged_restore_point_ids": ["p1", "p2", "p3"],
                "source_repository_id": "repo-src",
                "repository_id": "repo-dst",
                "disk_count": 2,
                "logical_bytes": 2048,
                "transferred_bytes": 512,
            },
        }

        report = abr_v41.session_to_dto(task)["synthetic_full"]

        self.assertEqual(report["merged_from"], 3)
        self.assertEqual(report["merged_restore_point_ids"], ["p1", "p2", "p3"])
        self.assertEqual(report["source_repository_name"], "Nightly")
        self.assertEqual(report["target_repository_name"], "Archive")
        self.assertEqual(report["disk_count"], 2)
        self.assertEqual(report["stored_bytes"], 512)

    def test_a_merge_inside_one_repository_reports_it_as_both_source_and_target(self) -> None:
        previous = REPOSITORIES.get("repo-1")
        REPOSITORIES["repo-1"] = {"id": "repo-1", "name": "Nightly", "type": "local"}
        self.addCleanup(lambda: REPOSITORIES.__setitem__("repo-1", previous) if previous else REPOSITORIES.pop("repo-1", None))
        task = {
            "id": "task-1",
            "task_type": "backup",
            "status": "completed",
            "result": {
                "synthetic_full": True,
                "merged_from": 2,
                "repository_id": "repo-1",
                "source_repository_id": "",
            },
        }

        report = abr_v41.session_to_dto(task)["synthetic_full"]

        self.assertEqual(report["source_repository_id"], "repo-1")
        self.assertEqual(report["target_repository_id"], "repo-1")

    def test_an_ordinary_backup_session_carries_no_synthetic_report(self) -> None:
        task = {
            "id": "task-1",
            "task_type": "backup",
            "status": "completed",
            "result": {"resolved_backup_type": "ActiveFull"},
        }

        self.assertNotIn("synthetic_full", abr_v41.session_to_dto(task))

    def test_a_synthetic_full_registers_as_a_full_and_opens_its_own_segment(self) -> None:
        first = self._register("ActiveFull", "task-1")
        self._register("Incremental", "task-2")
        synthetic = self._register("SyntheticFull", "task-3")

        self.assertTrue(synthetic.get("full_backup"))
        self.assertEqual(synthetic.get("segment_id"), synthetic.get("id"))
        self.assertNotEqual(synthetic.get("segment_id"), first.get("segment_id"))

    def test_an_incremental_still_records_its_parent(self) -> None:
        first = self._register("ActiveFull", "task-1")
        incremental = self._register("Incremental", "task-2")

        self.assertEqual(incremental.get("parent_point_id"), first.get("id"))


if __name__ == "__main__":
    unittest.main()
