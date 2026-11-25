# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ (AltaStor.com)

from __future__ import annotations

import contextlib
import errno
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from python.srv.core import storage_tool
from python.srv.plugins.hyperv import hyperv_backup, vhdx_tool


def _disk(slot: str = "SCSI 0:0") -> dict:
    return {
        "slot": slot,
        "supported": True,
        "unsupported_reason": "",
        "checkpoint_path": r"C:\VMs\et-vm\disk.avhdx",
        "datastore_path": r"C:\VMs\et-vm\disk.vhdx",
        "uuid": "disk-uuid",
        "capacity_bytes": 1 << 30,
    }


class FakeNode:
    cluster_name = None

    def __init__(self) -> None:
        self.removed_reference_points: list[str] = []

    def ensure_vm_host_connected(self, vm_id, hint=None):
        return {"name": "et-vm", "computer": "NODE-A", "state": "Off", "version": "12.0"}

    def get_service_type(self):
        return "Standalone"

    def ensure_disk_tool(self, node):
        return r"C:\Program Files\AltaStor\abr-vhdx-tool.exe"

    def create_recovery_checkpoint(self, vm_id, consistency="application"):
        return {"checkpoint_id": "cp-1", "computer": "NODE-A"}

    def checkpoint_disks(self, vm_id, checkpoint_id):
        return [_disk()]

    def get_vm_restore_metadata(self, vm_id):
        return {"name": "et-vm"}

    def convert_to_reference_point(self, vm_id, checkpoint_id):
        return {"reference_point_id": "rp-new"}

    def remove_reference_point(self, vm_id, reference_point_id):
        self.removed_reference_points.append(reference_point_id)


class HyperVBackupCompletionTests(unittest.TestCase):
    def _run(self, parameters: dict, previous: dict | None, *, flush_error: OSError | None = None):
        node = FakeNode()
        task = {"id": "task-2", "job_id": "job-1", "hypervisor_id": "inv-1", "parameters": parameters}
        repository = {"id": "repo-1", "name": "repo", "type": "local"}
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        destination_root, task_root = tmp / "repo", tmp / "repo" / "et-vm" / "task-2"
        task_root.mkdir(parents=True)
        (task_root / "01-disk.vhdx").write_bytes(b"data")
        outputs = [{"index": 1, "slot": "SCSI 0:0", "change_id": "rct-2", "previous_change_id": "rct-1" if previous else "", "logical_bytes": 4, "stored_bytes": 4}]
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(hyperv_backup, "_get_hypervisor", return_value={"name": "HyperV-test"}))
            stack.enter_context(patch.object(hyperv_backup, "connection_settings", return_value={"host": "hv.example.test"}))
            stack.enter_context(
                patch.object(
                    hyperv_backup, "_resolve_backup_target", return_value=({"external_id": "vm-1", "name": "et-vm", "payload": {}}, [])
                )
            )
            stack.enter_context(patch.object(hyperv_backup, "_resolve_repository", return_value=repository))
            stack.enter_context(patch.object(hyperv_backup.vhdx_tool, "ensure_hyperv_repository"))
            stack.enter_context(patch.object(hyperv_backup.vhdx_tool, "storage_invocation", return_value=([], {})))
            stack.enter_context(
                patch.object(
                    hyperv_backup, "_resolve_backup_storage_layout", return_value=(destination_root, task_root, "et-vm/task-2")
                )
            )
            stack.enter_context(
                patch.object(
                    hyperv_backup,
                    "_previous_manifest",
                    return_value=(destination_root / "et-vm" / "task-1" / "manifest.json", previous) if previous else (None, None),
                )
            )
            stack.enter_context(patch.object(hyperv_backup, "_repository_label", return_value="repo"))
            stack.enter_context(patch.object(hyperv_backup, "resolve_job_backup_mode", return_value=None))
            stack.enter_context(patch.object(hyperv_backup, "encryption_changed_since_last_backup", return_value=False))
            stack.enter_context(patch.object(hyperv_backup, "_remove_stranded_checkpoints"))
            stack.enter_context(patch.object(hyperv_backup, "_check_repository_access"))
            stack.enter_context(patch.object(hyperv_backup, "_back_up_disks", return_value=outputs))
            stack.enter_context(patch.object(hyperv_backup, "run_disk_tool"))
            stack.enter_context(patch.object(hyperv_backup, "apply_repository_immutability"))
            stack.enter_context(patch.object(hyperv_backup, "cancel_requested", return_value=False))
            stack.enter_context(patch.object(hyperv_backup, "open_node", return_value=contextlib.nullcontext(node)))
            discard = stack.enter_context(patch.object(hyperv_backup, "_discard_task_backups", return_value=[]))
            durable = stack.enter_context(
                patch.object(
                    hyperv_backup,
                    "make_durable",
                    side_effect=flush_error or storage_tool.make_durable,
                )
            )
            fsynced: list[str] = []
            real_fsync = storage_tool._fsync_path
            stack.enter_context(
                patch.object(storage_tool, "_fsync_path", side_effect=lambda path: (fsynced.append(Path(path).name), real_fsync(path)))
            )
            if flush_error is None:
                result = hyperv_backup.run_hyperv_backup(task)
            else:
                with self.assertLogs(hyperv_backup.logger, level="ERROR"):
                    with self.assertRaises(hyperv_backup.HyperVBackupError) as raised:
                        hyperv_backup.run_hyperv_backup(task)
                result = raised.exception
        return result, task_root, destination_root, durable, discard, fsynced

    def test_an_incremental_reports_the_restore_point_it_continued_from(self) -> None:
        previous = {"job_id": "job-1", "reference_point_id": "rp-old", "disks": [{"index": 1, "uuid": "disk-uuid", "change_id": "rct-1"}]}
        parameters = {"resolved_backup_type": "Incremental", "parent_point_id": "point-1", "parent_task_id": "task-1"}

        result, task_root, _root, _durable, _discard, _fsynced = self._run(parameters, previous)

        manifest = json.loads((task_root / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual((result["resolved_backup_type"], result["parent_point_id"]), ("Incremental", "point-1"))
        self.assertEqual(manifest["parent_point_id"], "point-1")

    def test_a_full_reports_no_base_even_with_a_bound_parent(self) -> None:
        parameters = {"resolved_backup_type": "Incremental", "parent_point_id": "point-1", "parent_task_id": "task-1"}

        result, task_root, _root, _durable, _discard, _fsynced = self._run(parameters, None)

        manifest = json.loads((task_root / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual((result["resolved_backup_type"], result["parent_point_id"]), ("ActiveFull", None))
        self.assertIsNone(manifest["parent_point_id"])

    def test_the_finished_backup_is_flushed_to_stable_storage_before_it_completes(self) -> None:
        result, task_root, destination_root, durable, discard, fsynced = self._run({"resolved_backup_type": "ActiveFull"}, None)

        durable.assert_called_once_with(task_root, up_to=destination_root)
        self.assertEqual(result["manifest_path"], str(task_root / "manifest.json"))
        self.assertFalse(list(task_root.glob(".*.tmp")))
        self.assertTrue({"manifest.json", "01-disk.vhdx", "task-2", "et-vm", "repo"} <= set(fsynced))
        discard.assert_not_called()

    def test_a_backup_that_cannot_be_flushed_fails_and_is_discarded(self) -> None:
        error, task_root, _root, _durable, discard, _fsynced = self._run(
            {"resolved_backup_type": "ActiveFull"}, None, flush_error=OSError(5, "Input/output error")
        )

        self.assertIn("could not be flushed to stable storage", str(error))
        self.assertIn(str(task_root), str(error))
        discard.assert_called_once()

    def test_a_backup_the_repository_had_no_room_to_flush_says_it_ran_out_of_space(self) -> None:
        error, _task_root, _root, _durable, discard, _fsynced = self._run(
            {"resolved_backup_type": "ActiveFull"}, None, flush_error=OSError(errno.ENOSPC, "No space left on device")
        )

        self.assertIn("The repository 'repo' ran out of space", str(error))
        self.assertNotIn("Errno", str(error))
        discard.assert_called_once()


TOOL_WRITE_ERROR = (
    "Virtual disk 'C:\\VMs\\et-vm\\disk.vhdx' attached read-only as \\\\.\\PhysicalDrive3; "
    "Write error, expected 4194410 bytes, written 4256589585 bytes!; failed to save data range at offset 62914560"
)


class _FailingNode:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines

    def run_disk_tool(self, node, args, **kwargs):
        return {"exit_code": 1, "result": None, "lines": self.lines}


class OutOfSpaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = self.enterContext(tempfile.TemporaryDirectory())
        self.repository = {"id": "repo-1", "name": "Shared", "type": "smb", "path": self.root}

    def _free(self, free_bytes: int | None):
        return patch.object(hyperv_backup, "filesystem_free_bytes", return_value=free_bytes)

    def _back_up(self, lines: list[str], free_bytes: int | None, *, allocated: int = 0, incremental: bool = False) -> str:
        with (
            self._free(free_bytes),
            patch.object(hyperv_backup, "cancel_requested", return_value=False),
            self.assertRaises(hyperv_backup.HyperVBackupError) as raised,
        ):
            hyperv_backup._back_up_disks(
                _FailingNode(lines),
                "NODE-A",
                [{**_disk(), "capacity_gb": 1, "allocated_bytes": allocated}],
                task_id="task-1",
                vm_name="et-vm",
                incremental=incremental,
                change_ids={"SCSI 0:0": "rct-1"} if incremental else {},
                storage_args=[],
                storage_env={},
                repository=self.repository,
                abr_path=self.root,
                repository_label="Shared",
                compression_level=3,
                encryption_key="",
                tool_timeout_seconds=900,
                written_backup_names=[],
                progress_callback=None,
            )
        return str(raised.exception)

    def test_a_write_error_on_a_full_share_says_the_repository_ran_out_of_space(self) -> None:
        message = self._back_up([f"Error: {TOOL_WRITE_ERROR}"], 40 * 1024**2)

        self.assertEqual(
            message,
            f"Backing up disk SCSI 0:0 of et-vm failed: The repository 'Shared' at {self.root} ran out of space "
            "(40.0 MiB free; 40.0 MiB free when this disk started). Free space in that filesystem, or point the job "
            "at another repository",
        )

    def test_a_full_share_names_the_room_the_disk_takes_before_compression(self) -> None:
        message = self._back_up([f"Error: {TOOL_WRITE_ERROR}"], 0, allocated=3 * 1024**3)

        self.assertIn("ran out of space (0 B free; 0 B free when this disk started; the disk takes 3.0 GiB on the host "
                      "before compression)", message)
        self.assertNotIn("Write error", message)

    def test_the_room_the_disk_takes_is_left_out_of_an_incremental(self) -> None:
        message = self._back_up([f"Error: {TOOL_WRITE_ERROR}"], 0, allocated=3 * 1024**3, incremental=True)

        self.assertIn("ran out of space", message)
        self.assertNotIn("the disk takes", message)

    def test_a_write_error_with_room_left_never_shows_the_tools_write_count(self) -> None:
        for free in (261 * 1024**2, None):
            with self.subTest(free=free):
                message = self._back_up([f"Error: {TOOL_WRITE_ERROR}"], free)

                self.assertNotIn("ran out of space", message)
                self.assertNotIn("Write error", message)
                self.assertNotIn("4256589585", message)
                self.assertIn("attached read-only as \\\\.\\PhysicalDrive3; failed to save data range at offset 62914560", message)

    def test_a_tool_that_says_the_disk_is_full_is_believed_whatever_the_free_space(self) -> None:
        message = self._back_up(["Error: There is not enough space on the disk."], None)

        self.assertIn("The repository 'Shared'", message)
        self.assertIn("ran out of space", message)

    def test_another_tool_failure_on_a_full_share_is_not_called_a_space_problem(self) -> None:
        message = self._back_up(["Error: Access is denied."], 0)

        self.assertIn("Access is denied", message)
        self.assertNotIn("ran out of space", message)

    def test_a_session_folder_the_share_has_no_room_for_says_the_repository_ran_out_of_space(self) -> None:
        full = OSError(errno.ENOSPC, "No space left on device")
        with (
            self._free(0),
            patch.object(hyperv_backup, "ensure_private_directory", side_effect=full),
            self.assertRaises(hyperv_backup.HyperVBackupError) as raised,
        ):
            hyperv_backup._make_task_root(self.repository, Path(self.root) / "vm" / "task-1")

        self.assertEqual(
            str(raised.exception),
            f"The repository 'Shared' at {self.root} ran out of space (0 B free). "
            "Free space in that filesystem, or point the job at another repository",
        )

    def test_a_session_folder_that_fails_for_another_reason_keeps_its_error(self) -> None:
        denied = PermissionError(errno.EACCES, "Permission denied")
        with (
            patch.object(hyperv_backup, "ensure_private_directory", side_effect=denied),
            self.assertRaises(PermissionError),
        ):
            hyperv_backup._make_task_root(self.repository, Path(self.root) / "vm" / "task-1")


SHARE = "\\\\files\\abr-x$"
WRITTEN = {"exit_code": 0, "result": {"status": "ok", "raw_data_size": 100, "stored_data_size": 50, "rct_id": "rct-2"}, "lines": []}
WRITE_FAILED = {"exit_code": 1, "result": None, "lines": [f"Error: {TOOL_WRITE_ERROR}"]}
WRITE_FAILED_LATE = {
    "exit_code": 1,
    "result": None,
    "lines": [f"Error: {TOOL_WRITE_ERROR.replace('at offset 62914560', 'at offset 2147483648')}"],
}
OPENS = {"exit_code": 0, "result": {"status": "ok"}, "lines": []}
CLOSED = {"exit_code": 1, "result": None, "lines": ["Error: NODE-A cannot open the repository share \\\\files\\abr-x$ as abr: The network name cannot be found."]}


def _full_disk() -> dict:
    return {
        **_disk(),
        "capacity_gb": 1,
        "allocated_bytes": 1 << 28,
        "controller_type": "SCSI",
        "controller_number": 0,
        "controller_location": 0,
        "chain_paths": [],
        "logical_sector_size": 512,
        "physical_sector_size": 4096,
        "block_size": 1 << 25,
        "vhd_format": "VHDX",
        "vhd_type": "Dynamic",
        "source_disk_type_code": 3,
        "source_disk_type_name": "Dynamic",
        "source_disk_eager_zeroed": False,
    }


class _ScriptedNode:
    def __init__(self, backups: list[dict], probes: list[dict] | None = None) -> None:
        self.backups = list(backups)
        self.probes = list(probes or [])
        self.calls: list[str] = []

    def run_disk_tool(self, node, args, **kwargs):
        self.calls.append(args[0])
        if args[0] == "info":
            return self.probes.pop(0) if self.probes else OPENS
        return self.backups.pop(0)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def time(self) -> float:
        return self.now


class _LossCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = self.enterContext(tempfile.TemporaryDirectory())
        self.repository = {"id": "repo-1", "name": "Shared", "type": "smb", "path": self.root}
        self.notices: list[str] = []
        self.removed: list[str] = []
        self.clock = _Clock()
        self.cancel = False

    def _run(
        self,
        node: _ScriptedNode,
        *,
        attempts: int = 3,
        window: int = 300,
        incremental: bool = False,
        free: int = 50 * 1024**3,
        abr_path: str | None = None,
        free_by_path: dict[str, int] | None = None,
    ):
        def progress(payload: dict) -> None:
            if payload.get("log_level") == "warn":
                self.notices.append(str(payload.get("message")))

        def free_bytes(path: str) -> int:
            return (free_by_path or {}).get(str(path), free)

        with (
            patch.dict("os.environ", {"ALTASTOR_HYPERV_STORAGE_ATTEMPTS": str(attempts)}),
            patch.object(hyperv_backup, "filesystem_free_bytes", side_effect=free_bytes),
            patch.object(hyperv_backup, "cancel_requested", side_effect=lambda _task: self.cancel),
            patch.object(hyperv_backup, "reconnect_seconds", return_value=window),
            patch.object(hyperv_backup, "remove_disk_tool_backup", side_effect=lambda _repo, _path, name, **_kw: self.removed.append(name)),
            patch.object(hyperv_backup, "time", self.clock),
        ):
            return hyperv_backup._back_up_disks(
                node,
                "NODE-A",
                [_full_disk()],
                task_id="task-1",
                vm_name="et-vm",
                incremental=incremental,
                change_ids={"SCSI 0:0": "rct-1"} if incremental else {},
                storage_args=["--mode", "file", "--folder", f"{SHARE}\\vm\\task-1"],
                storage_env={"ABR_SMB_SHARE": SHARE},
                repository=self.repository,
                abr_path=abr_path or self.root,
                repository_label="Shared",
                compression_level=3,
                encryption_key="",
                tool_timeout_seconds=900,
                written_backup_names=[],
                progress_callback=progress,
            )

    def _fails(self, node: _ScriptedNode, **kwargs) -> str:
        with self.assertRaises(hyperv_backup.HyperVBackupError) as raised:
            self._run(node, **kwargs)
        return str(raised.exception)


class ShareLossTests(_LossCase):
    def test_a_share_lost_and_back_by_the_time_the_tool_fails_writes_the_disk_again(self) -> None:
        node = _ScriptedNode([WRITE_FAILED, WRITTEN])

        outputs = self._run(node)

        self.assertEqual(node.calls, ["backup", "info", "backup"])
        self.assertEqual(outputs[0]["change_id"], "rct-2")
        self.assertEqual(self.removed, ["01-disk.vhdx"])
        self.assertEqual(
            self.notices,
            [
                (
                    f"NODE-A lost the repository share {SHARE} while writing disk SCSI 0:0 of et-vm; it opens again, "
                    "so the disk is backed up again from the start (attempt 2 of 3)"
                )
            ],
        )

    def test_a_share_still_lost_is_waited_for_and_then_the_disk_is_written_again(self) -> None:
        node = _ScriptedNode([WRITE_FAILED, WRITTEN], [CLOSED, CLOSED, OPENS])

        self._run(node, incremental=True)

        self.assertEqual(node.calls, ["backup", "info", "info", "info", "backup"])
        self.assertEqual(
            self.notices[0],
            f"NODE-A lost the repository share {SHARE} while writing disk SCSI 0:0 of et-vm; waiting up to 300 s for it "
            "to open again",
        )
        self.assertIn("it opened again after 10 s, so the disk is backed up again from the start (attempt 2 of 3)", self.notices[1])

    def test_a_share_that_does_not_come_back_within_the_window_says_so(self) -> None:
        message = self._fails(_ScriptedNode([WRITE_FAILED], [CLOSED] * 100), window=30)

        self.assertEqual(
            message,
            f"Backing up disk SCSI 0:0 of et-vm failed: NODE-A lost the repository share {SHARE} while writing disk "
            "SCSI 0:0 of et-vm, and could not open it again within 30 s. Check the network between NODE-A and the file server",
        )
        self.assertEqual(self.removed, [])

    def test_a_disk_that_fails_on_every_attempt_stops_after_the_last_one(self) -> None:
        node = _ScriptedNode([WRITE_FAILED, WRITE_FAILED])

        message = self._fails(node, attempts=2)

        self.assertEqual(node.calls, ["backup", "info", "backup", "info"])
        self.assertEqual(
            message,
            f"Backing up disk SCSI 0:0 of et-vm failed: NODE-A lost the repository share {SHARE} while writing disk "
            "SCSI 0:0 of et-vm; it opened again each time, but the disk could not be written on any of 2 attempts",
        )
        self.assertNotIn("Write error", message)

    def test_a_network_error_from_the_tool_counts_as_a_lost_share(self) -> None:
        dropped = {"exit_code": 1, "result": None, "lines": ["Error: The specified network name is no longer available."]}
        node = _ScriptedNode([dropped, WRITTEN])

        self._run(node)

        self.assertEqual(node.calls, ["backup", "info", "backup"])

    def test_a_failure_that_is_not_a_write_is_not_retried(self) -> None:
        denied = {"exit_code": 1, "result": None, "lines": ["Error: Access is denied."]}
        node = _ScriptedNode([denied])

        message = self._fails(node)

        self.assertEqual(node.calls, ["backup"])
        self.assertEqual(message, "Backing up disk SCSI 0:0 of et-vm failed: Access is denied.")

    def test_an_s3_repository_is_not_waited_for(self) -> None:
        self.repository = {"id": "repo-1", "name": "Bucket", "type": "s3", "path": "s3://bucket/abr"}
        node = _ScriptedNode([WRITE_FAILED])

        message = self._fails(node)

        self.assertEqual(node.calls, ["backup"])
        self.assertNotIn("lost the repository share", message)
        self.assertNotIn("Write error", message)

    def test_a_full_share_is_not_retried(self) -> None:
        node = _ScriptedNode([WRITE_FAILED])

        message = self._fails(node, free=1024)

        self.assertIn("ran out of space (1.0 KiB free; 1.0 KiB free when this disk started; the disk takes", message)
        self.assertEqual(node.calls, ["backup"])

    def test_a_disk_that_failed_past_the_room_the_share_had_while_the_share_opens_ran_out_of_space(self) -> None:
        node = _ScriptedNode([WRITE_FAILED_LATE])

        message = self._fails(node, free=500 * 1024**2)

        self.assertEqual(node.calls, ["backup", "info"])
        self.assertEqual(
            message,
            f"Backing up disk SCSI 0:0 of et-vm failed: The repository 'Shared' at {self.root} ran out of space "
            "(500.0 MiB free when this disk started; the disk takes 256.0 MiB on the host before compression). "
            "Free space in that filesystem, or point the job at another repository",
        )

    def test_the_same_failure_while_the_share_does_not_open_is_a_lost_share(self) -> None:
        node = _ScriptedNode([WRITE_FAILED_LATE, WRITTEN], [CLOSED, OPENS])

        self._run(node, free=500 * 1024**2)

        self.assertEqual(node.calls, ["backup", "info", "info", "backup"])
        self.assertIn("attempt 2 of 3", self.notices[-1])

    def test_the_free_space_is_read_where_the_point_is_written_not_at_the_repository_root(self) -> None:
        folder = str(Path(self.root) / "et-vm" / "task-1")
        node = _ScriptedNode([WRITE_FAILED])

        message = self._fails(node, abr_path=folder, free_by_path={folder: 2048})

        self.assertIn(f"The repository 'Shared' at {folder} ran out of space (2.0 KiB free", message)
        self.assertEqual(node.calls, ["backup"])

    def test_a_stop_while_waiting_for_the_share_stops_the_session(self) -> None:
        node = _ScriptedNode([WRITE_FAILED], [CLOSED, CLOSED])
        original = node.run_disk_tool

        def probe_then_stop(n, args, **kwargs):
            result = original(n, args, **kwargs)
            if args[0] == "info":
                self.cancel = True
            return result

        node.run_disk_tool = probe_then_stop

        with self.assertRaises(hyperv_backup.ProcessCancelled):
            self._run(node)
        self.assertEqual(self.removed, [])


S3_DOWN_LINE = (
    "Error: Virtual disk 'C:\\VMs\\et-vm\\disk.vhdx' attached read-only as \\\\.\\PhysicalDrive3; S3 bucket 'et-bucket' is "
    "not accessible at endpoint http://s3.example:9000: Encountered network error when sending http request. Create the "
    "bucket or enable bucket auto-creation.; Failed to initialize backup manager!; failed to initialize 's3' backup "
    "storage at 'et-bucket'"
)
S3_DOWN = {"exit_code": 1, "result": None, "lines": [S3_DOWN_LINE]}
S3_CLOSED = {"exit_code": 1, "result": None, "lines": [S3_DOWN_LINE.replace("Virtual disk 'C:\\VMs\\et-vm\\disk.vhdx' attached read-only as \\\\.\\PhysicalDrive3; ", "")]}
S3_NO_BUCKET = {
    "exit_code": 1,
    "result": None,
    "lines": ["Error: S3 bucket 'et-bucket' does not exist at endpoint http://s3.example:9000. Create the bucket or enable bucket auto-creation."],
}


def _s3_repository() -> dict:
    return {
        "id": "repo-1",
        "name": "Bucket",
        "type": "s3",
        "path": "s3://et-bucket/et",
        "s3": {"bucket": "et-bucket", "endpoint_url": "http://s3.example:9000"},
    }


class S3LossTests(_LossCase):
    def setUp(self) -> None:
        super().setUp()
        self.repository = _s3_repository()

    def test_a_storage_that_stops_answering_is_waited_for_and_the_disk_is_written_again(self) -> None:
        node = _ScriptedNode([S3_DOWN, WRITTEN], [S3_CLOSED, OPENS])

        self._run(node)

        self.assertEqual(node.calls, ["backup", "info", "info", "backup"])
        lost = "NODE-A lost the S3 storage http://s3.example:9000 while writing disk SCSI 0:0 of et-vm"
        self.assertEqual(
            self.notices,
            [
                f"{lost}; waiting up to 300 s for it to answer again",
                f"{lost}; it answered again after 5 s, so the disk is backed up again from the start (attempt 2 of 3)",
            ],
        )

    def test_a_storage_back_by_the_time_the_tool_fails_writes_the_disk_again_at_once(self) -> None:
        node = _ScriptedNode([S3_DOWN, WRITTEN])

        self._run(node, incremental=True)

        self.assertEqual(node.calls, ["backup", "info", "backup"])
        self.assertIn("it answers again, so the disk is backed up again from the start (attempt 2 of 3)", self.notices[0])

    def test_a_storage_that_does_not_answer_within_the_window_says_so_without_the_tools_advice(self) -> None:
        message = self._fails(_ScriptedNode([S3_DOWN], [S3_CLOSED] * 100), window=30)

        self.assertEqual(
            message,
            "Backing up disk SCSI 0:0 of et-vm failed: NODE-A lost the S3 storage http://s3.example:9000 while writing disk "
            "SCSI 0:0 of et-vm, and could not reach it again within 30 s. Check the network between NODE-A and the S3 "
            "storage",
        )
        self.assertNotIn("Create the bucket", message)
        self.assertNotIn("Failed to initialize", message)

    def test_a_storage_that_fails_on_every_attempt_stops_after_the_last_one(self) -> None:
        message = self._fails(_ScriptedNode([S3_DOWN, S3_DOWN]), attempts=2)

        self.assertIn("it answered again each time, but the disk could not be written on any of 2 attempts", message)

    def test_a_missing_bucket_is_not_waited_for(self) -> None:
        node = _ScriptedNode([S3_NO_BUCKET])

        message = self._fails(node)

        self.assertEqual(node.calls, ["backup"])
        self.assertIn("does not exist", message)

    def test_a_stop_while_waiting_for_the_storage_stops_the_session(self) -> None:
        node = _ScriptedNode([S3_DOWN], [S3_CLOSED, S3_CLOSED])
        original = node.run_disk_tool

        def probe_then_stop(n, args, **kwargs):
            result = original(n, args, **kwargs)
            if args[0] == "info":
                self.cancel = True
            return result

        node.run_disk_tool = probe_then_stop

        with self.assertRaises(hyperv_backup.ProcessCancelled):
            self._run(node)


class _InfoNode:
    def __init__(self, run: dict) -> None:
        self.run = run

    def run_disk_tool(self, node, args, **kwargs):
        return self.run


class RepositoryCheckS3Tests(unittest.TestCase):
    def _check(self, run: dict) -> str:
        with (
            patch.object(hyperv_backup, "cancel_requested", return_value=False),
            self.assertRaises(hyperv_backup.HyperVBackupError) as raised,
        ):
            hyperv_backup._check_repository_access(
                _InfoNode(run), "NODE-A", [], {}, "Bucket", "task-1", None, repository=_s3_repository()
            )
        return str(raised.exception)

    def test_a_storage_that_does_not_answer_before_the_run_is_not_called_a_missing_bucket(self) -> None:
        message = self._check(S3_CLOSED)

        self.assertEqual(
            message,
            "NODE-A cannot reach repository Bucket: the S3 storage http://s3.example:9000 did not answer (a network error). "
            "Check the network between NODE-A and the S3 storage",
        )

    def test_the_endpoint_comes_from_the_tool_then_the_repository_then_the_bucket(self) -> None:
        bare = {"id": "repo-1", "name": "Bucket", "type": "s3", "s3": {"bucket": "et-bucket"}}
        cases = [
            (_s3_repository(), "network error at endpoint https://s3.other:443. Retry", "https://s3.other:443"),
            (_s3_repository(), "Encountered network error", "http://s3.example:9000"),
            (bare, "Encountered network error", "of bucket et-bucket"),
        ]
        for repository, text, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(hyperv_backup.s3_endpoint(repository, text), expected)

    def test_a_missing_bucket_before_the_run_keeps_the_tools_words(self) -> None:
        self.assertIn("does not exist", self._check(S3_NO_BUCKET))


INFO_LINE ="Virtual disk 'C:\\VMs\\et-vm\\disk.vhdx' attached read-only as \\\\.\\PhysicalDrive3"


class _EndedNode:
    def __init__(self, run: dict) -> None:
        self.run = run

    def run_disk_tool(self, node, args, **kwargs):
        return self.run


class ToolEndedUnexpectedlyTests(unittest.TestCase):
    def _back_up(self, run: dict) -> str:
        with patch.object(hyperv_backup, "cancel_requested", return_value=False), patch.object(
            hyperv_backup, "filesystem_free_bytes", return_value=50 * 1024**3
        ), self.assertRaises(hyperv_backup.HyperVBackupError) as raised:
            hyperv_backup._back_up_disks(
                _EndedNode(run),
                "NODE-A",
                [{**_disk(), "capacity_gb": 1}],
                task_id="task-1",
                vm_name="et-vm",
                incremental=False,
                change_ids={},
                storage_args=[],
                storage_env={},
                repository={"id": "repo-1", "name": "Shared", "type": "smb", "path": "/mnt/hv"},
                abr_path="/mnt/hv/et-vm/task-1",
                repository_label="Shared",
                compression_level=3,
                encryption_key="",
                tool_timeout_seconds=900,
                written_backup_names=[],
                progress_callback=None,
            )
        return str(raised.exception)

    def test_a_tool_killed_on_the_host_is_named_as_ended_unexpectedly(self) -> None:
        message = self._back_up({"exit_code": -1, "result": None, "lines": [INFO_LINE, "45%", "46%"]})

        self.assertEqual(
            message,
            "abr-vhdx-tool on NODE-A ended unexpectedly (exit code -1) while backing up disk SCSI 0:0 of et-vm. "
            f"Its last output: {INFO_LINE}",
        )

    def test_a_tool_that_reports_its_own_error_keeps_its_words(self) -> None:
        message = self._back_up(
            {"exit_code": 1, "result": {"status": "error", "error": "failed to create backup"}, "lines": [INFO_LINE]}
        )

        self.assertTrue(message.startswith("Backing up disk SCSI 0:0 of et-vm failed: "), message)
        self.assertIn("failed to create backup", message)
        self.assertNotIn("ended unexpectedly", message)

    def test_an_error_line_without_a_result_is_still_the_tools_own_error(self) -> None:
        message = self._back_up({"exit_code": 1, "result": None, "lines": [INFO_LINE, "Error: Access is denied."]})

        self.assertIn("Access is denied", message)
        self.assertNotIn("ended unexpectedly", message)

    def test_the_end_is_recognised_only_without_a_result_or_an_error_line(self) -> None:
        self.assertTrue(vhdx_tool.ended_unexpectedly(None, [INFO_LINE], -1))
        self.assertTrue(vhdx_tool.ended_unexpectedly(None, [], 3221225477))
        self.assertFalse(vhdx_tool.ended_unexpectedly(None, [INFO_LINE], 0))
        self.assertFalse(vhdx_tool.ended_unexpectedly(None, [INFO_LINE], None))
        self.assertFalse(vhdx_tool.ended_unexpectedly({"status": "error"}, [], 1))
        self.assertFalse(vhdx_tool.ended_unexpectedly(None, ["Error: broken"], 1))

    def test_a_crash_code_is_shown_in_hex_and_an_empty_run_says_so(self) -> None:
        self.assertEqual(
            vhdx_tool.unexpected_end_message("NODE-A", 3221225477, ["ABR_TOOL_EXIT=3221225477"], "restoring disk SCSI0:0"),
            "abr-vhdx-tool on NODE-A ended unexpectedly (exit code 3221225477, 0xC0000005) while restoring disk SCSI0:0. "
            "It printed nothing",
        )


if __name__ == "__main__":
    unittest.main()


class ConsistencyReasonTests(unittest.TestCase):
    _run = HyperVBackupCompletionTests._run

    def _backup(self, parameters: dict, state: str, refuse_application: bool = False) -> tuple[dict, dict, list]:
        asked: list[str] = []

        def checkpoint(node, vm_id, consistency="application"):
            asked.append(consistency)
            if refuse_application and consistency == "application":
                raise hyperv_backup.HyperVError("The guest does not answer the Backup (volume shadow copy) service")
            return {"checkpoint_id": "cp-1", "computer": "NODE-A"}

        running = {"name": "et-vm", "computer": "NODE-A", "state": state, "version": "12.0"}
        with patch.object(FakeNode, "ensure_vm_host_connected", return_value=running), patch.object(
            FakeNode, "create_recovery_checkpoint", checkpoint
        ):
            result, task_root, *_ = self._run(parameters, None)
        manifest = json.loads((task_root / "manifest.json").read_text(encoding="utf-8"))
        return result, manifest, asked

    def _warnings(self, parameters: dict, state: str, refuse_application: bool = False) -> list[dict]:
        sent: list[dict] = []

        def emit(callback, stage, progress, message, **extra):
            if isinstance(extra.get("warning"), dict):
                sent.append(extra["warning"])

        with patch.object(hyperv_backup, "_emit_progress", side_effect=emit):
            self._backup(parameters, state, refuse_application=refuse_application)
        return sent

    def test_the_crash_consistent_fallback_warning_carries_its_code_and_the_vm(self) -> None:
        warnings = self._warnings({}, "Running", refuse_application=True)

        [warning] = [w for w in warnings if w["code"] == "crash_consistent"]
        self.assertEqual(warning["vm_name"], "et-vm")
        self.assertIn("taking a crash-consistent one instead", warning["message"])

    def test_every_warning_of_a_run_has_a_code_a_message_and_the_vm(self) -> None:
        warnings = self._warnings({"resolved_backup_type": "Incremental"}, "Running", refuse_application=True)

        self.assertTrue(warnings)
        for warning in warnings:
            self.assertEqual(set(warning), {"code", "message", "vm_name"})
            self.assertTrue(warning["code"] and warning["message"])
        self.assertIn("no_previous_backup", [w["code"] for w in warnings])

    def test_a_guest_that_cannot_do_vss_says_so_on_the_session_and_the_point(self) -> None:
        result, manifest, asked = self._backup({}, "Running", refuse_application=True)

        self.assertEqual(asked, ["application", "crash"])
        self.assertEqual((result["consistency"], result["consistency_reason"]), ("crash", "vss_failed"))
        self.assertEqual(manifest["consistency_reason"], "vss_failed")
        self.assertTrue(any("crash-consistent one instead" in warning for warning in result["warnings"]))

    def test_on_quiesce_failure_fail_ends_the_session_with_no_point_and_names_the_setting(self) -> None:
        for value in ("fail",):
            with self.subTest(value):
                with self.assertRaises(hyperv_backup.HyperVBackupError) as caught:
                    self._backup({"on_quiesce_failure": value}, "Running", refuse_application=True)

                message = str(caught.exception)
                self.assertIn("Application-consistent checkpoint of et-vm failed", message)
                self.assertIn("--on-quiesce-failure fail", message)
                self.assertIn("Backup (volume shadow copy) integration service", message)

    def test_on_quiesce_failure_crash_consistent_falls_back_as_before(self) -> None:
        for value in ("crash_consistent", "crash-consistent"):
            with self.subTest(value):
                result, _manifest, asked = self._backup({"on_quiesce_failure": value}, "Running", refuse_application=True)

                self.assertEqual(asked, ["application", "crash"])
                self.assertEqual((result["consistency"], result["consistency_reason"]), ("crash", "vss_failed"))

    def test_on_quiesce_failure_fail_takes_an_application_checkpoint_when_vss_works(self) -> None:
        result, _manifest, asked = self._backup({"on_quiesce_failure": "fail"}, "Running")

        self.assertEqual((asked, result["consistency"]), (["application"], "application"))

    def test_on_quiesce_failure_fail_does_not_touch_a_vm_that_is_off(self) -> None:
        result, _manifest, asked = self._backup({"on_quiesce_failure": "fail"}, "Off")

        self.assertEqual((asked, result["consistency_reason"]), (["crash"], "vm_not_running"))

    def test_a_vm_that_is_not_running_says_it_had_no_guest_to_quiesce(self) -> None:
        result, manifest, asked = self._backup({}, "Off")

        self.assertEqual(asked, ["crash"])
        self.assertEqual((result["consistency"], result["consistency_reason"]), ("crash", "vm_not_running"))
        self.assertEqual(manifest["consistency_reason"], "vm_not_running")

    def test_an_application_consistent_backup_and_a_crash_job_need_no_reason(self) -> None:
        application, _manifest, _asked = self._backup({}, "Running")
        crash, _manifest, asked = self._backup({"hyperv_consistency": "crash"}, "Running")

        self.assertEqual((application["consistency"], application["consistency_reason"]), ("application", None))
        self.assertEqual((crash["consistency"], crash["consistency_reason"]), ("crash", None))
        self.assertEqual(asked, ["crash"])
