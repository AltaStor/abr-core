# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

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

    def _back_up(self, lines: list[str], free_bytes: int | None) -> str:
        with (
            self._free(free_bytes),
            patch.object(hyperv_backup, "cancel_requested", return_value=False),
            self.assertRaises(hyperv_backup.HyperVBackupError) as raised,
        ):
            hyperv_backup._back_up_disks(
                _FailingNode(lines),
                "NODE-A",
                [{**_disk(), "capacity_gb": 1}],
                task_id="task-1",
                vm_name="et-vm",
                incremental=False,
                change_ids={},
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
            "(40.0 MiB free when this disk started). Free space in that filesystem, or point the job at another repository",
        )

    def test_a_write_error_with_more_room_than_the_disk_had_reached_keeps_the_tools_own_words(self) -> None:
        message = self._back_up([f"Error: {TOOL_WRITE_ERROR}"], 261 * 1024**2)

        self.assertIn("Write error, expected 4194410 bytes", message)
        self.assertNotIn("ran out of space", message)

    def test_a_write_error_without_an_offset_or_a_free_space_figure_keeps_the_tools_own_words(self) -> None:
        for lines, free in (([f"Error: {TOOL_WRITE_ERROR.split('; failed')[0]}"], 1024), ([f"Error: {TOOL_WRITE_ERROR}"], None)):
            with self.subTest(free=free):
                message = self._back_up(lines, free)

                self.assertIn("Write error, expected 4194410 bytes", message)
                self.assertNotIn("ran out of space", message)

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


INFO_LINE = "Virtual disk 'C:\\VMs\\et-vm\\disk.vhdx' attached read-only as \\\\.\\PhysicalDrive3"


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

    def test_a_guest_that_cannot_do_vss_says_so_on_the_session_and_the_point(self) -> None:
        result, manifest, asked = self._backup({}, "Running", refuse_application=True)

        self.assertEqual(asked, ["application", "crash"])
        self.assertEqual((result["consistency"], result["consistency_reason"]), ("crash", "vss_failed"))
        self.assertEqual(manifest["consistency_reason"], "vss_failed")
        self.assertTrue(any("crash-consistent one instead" in warning for warning in result["warnings"]))

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
