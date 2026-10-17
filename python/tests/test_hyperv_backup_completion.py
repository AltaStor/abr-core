# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from python.srv.core import storage_tool
from python.srv.plugins.hyperv import hyperv_backup


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


if __name__ == "__main__":
    unittest.main()
