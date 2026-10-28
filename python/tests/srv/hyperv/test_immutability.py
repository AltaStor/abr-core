# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import BACKUPS, JOBS, REPOSITORIES
from python.srv.core.storage_tool import RETENTION_TILL_KEY
from python.srv.plugins.hyperv import hyperv_backup, vhdx_tool
from python.srv.services import abr_v41


DISK = {
    "slot": "SCSI0:0",
    "capacity_bytes": 1024**3,
    "capacity_gb": 1.0,
    "checkpoint_path": r"C:\vms\vm\disk.avhdx",
    "controller_type": "SCSI",
    "controller_number": 0,
    "controller_location": 0,
    "datastore_path": r"C:\vms\vm\disk.vhdx",
    "chain_paths": [],
    "uuid": "disk-1",
    "logical_sector_size": 512,
    "physical_sector_size": 4096,
    "block_size": 33554432,
    "vhd_format": "VHDX",
    "vhd_type": "Dynamic",
    "source_disk_type_code": 0,
    "source_disk_type_name": "dynamic",
    "source_disk_eager_zeroed": False,
}


class FakeNode:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def run_disk_tool(self, node, args, *, env=None, on_progress=None, should_cancel=None, timeout=None):
        self.commands.append(list(args))
        return {
            "exit_code": 0,
            "result": {"status": "ok", "raw_data_size": 4096, "stored_data_size": 4096, "rct_id": "rctX:1"},
            "lines": [],
        }


def _s3_repository(immutability: dict | None) -> dict:
    return {
        "id": "repo-1",
        "name": "Repo",
        "type": "s3",
        "s3": {"bucket": "bucket", "region": "us-east-1", "immutability": immutability},
    }


class HyperVDiskLockTests(unittest.TestCase):
    def _command(self, repository: dict) -> list[str]:
        node = FakeNode()
        with patch.object(hyperv_backup, "cancel_requested", return_value=False):
            hyperv_backup._back_up_disks(
                node,
                "NODE-A",
                [dict(DISK)],
                task_id="task-1",
                vm_name="vm",
                incremental=False,
                change_ids={},
                storage_args=["--mode", "s3"],
                storage_env={},
                repository=repository,
                abr_path="prefix/vm/task-1",
                repository_label="Repo",
                compression_level=3,
                encryption_key="",
                tool_timeout_seconds=0,
                written_backup_names=[],
                progress_callback=None,
            )
        self.assertEqual(len(node.commands), 1)
        return node.commands[0]

    def test_an_immutable_repository_locks_every_disk_the_host_writes(self) -> None:
        command = self._command(_s3_repository({"mode": "governance", "retention_days": 6}))

        index = command.index("--immutable")
        self.assertEqual(command[index : index + 4], ["--immutable", "governance", "--retention", "6"])

    def test_a_repository_without_immutability_writes_without_a_lock(self) -> None:
        command = self._command(_s3_repository(None))

        self.assertNotIn("--immutable", command)
        self.assertNotIn("--retention", command)

    def test_every_disk_is_held_to_the_chain_date_when_the_host_tool_takes_one(self) -> None:
        repository = dict(_s3_repository({"mode": "governance", "retention_days": 6}))
        repository[RETENTION_TILL_KEY] = "2026-10-25T00:00:00Z"
        with patch.object(vhdx_tool, "supports_retention_till", return_value=True):
            command = self._command(repository)

        index = command.index("--immutable")
        self.assertEqual(
            command[index : index + 4], ["--immutable", "governance", "--retention-till", "2026-10-25T00:00:00Z"]
        )

    def test_an_older_host_tool_gets_whole_days(self) -> None:
        repository = dict(_s3_repository({"mode": "governance", "retention_days": 6}))
        repository[RETENTION_TILL_KEY] = "2026-10-25T00:00:00Z"
        with patch.object(vhdx_tool, "supports_retention_till", return_value=False):
            command = self._command(repository)

        index = command.index("--immutable")
        self.assertEqual(command[index : index + 4], ["--immutable", "governance", "--retention", "6"])


class HostToolRetentionTillTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tool = Path(tmp.name) / "abr-vhdx-tool.exe"
        vhdx_tool._retention_till_support.clear()
        self.addCleanup(vhdx_tool._retention_till_support.clear)

    def _supports(self, content: bytes) -> bool:
        self.tool.write_bytes(content)
        with patch.object(vhdx_tool, "local_tool_path", return_value=self.tool):
            return vhdx_tool.supports_retention_till()

    def test_a_tool_that_names_the_option_takes_a_date(self) -> None:
        self.assertTrue(self._supports(b"MZ" + b"\0" * 100 + b"--retention-till DATE"))

    def test_the_option_split_across_two_reads_is_still_found(self) -> None:
        self.assertTrue(self._supports(b"\0" * ((1 << 20) - 5) + b"--retention-till" + b"\0" * 10))

    def test_a_tool_without_it_gets_days(self) -> None:
        self.assertFalse(self._supports(b"MZ" + b"\0" * 100 + b"--retention DAYS"))

    def test_without_an_appliance_copy_the_hosts_get_days(self) -> None:
        with patch.object(vhdx_tool, "local_tool_path", return_value=None):
            self.assertFalse(vhdx_tool.supports_retention_till())


class HyperVRetentionUnderObjectLockTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, JOBS, REPOSITORIES):
            store.clear()
        REPOSITORIES["repo-1"] = _s3_repository({"mode": "governance", "retention_days": 30})
        JOBS["job-1"] = {"id": "job-1", "name": "HV", "job_type": "backup", "repo_id": "repo-1", "retention_days": 1}
        for point_id, created_at, full, parent in (
            ("full-1", "2026-01-01T00:00:00Z", True, None),
            ("inc-1", "2026-01-02T00:00:00Z", False, "full-1"),
        ):
            BACKUPS[point_id] = {
                "id": point_id,
                "job_id": "job-1",
                "vm_id": "vm-1",
                "vm_name": "vm",
                "repository_id": "repo-1",
                "hypervisor_kind": "hyper-v",
                "status": "success",
                "full_backup": full,
                "execution_type": "ActiveFull" if full else "Incremental",
                "parent_point_id": parent,
                "created_at": created_at,
                "immutability_expires_at": "2999-01-01T00:00:00+00:00",
                "artifact": {"disks": [{"abr_path": f"prefix/vm/{point_id}", "abr_backup_name": "01-vm.vhdx"}]},
            }

    def tearDown(self) -> None:
        for store in (BACKUPS, JOBS, REPOSITORIES):
            store.clear()

    def test_expired_points_still_under_lock_are_held_and_never_deleted(self) -> None:
        report = abr_v41.new_retention_report(JOBS["job-1"], "vm-1", "repo-1")
        locked = {"immutability": "governance", "retention_days": 30}
        with patch.object(abr_v41, "remove_disk_tool_backup") as remove, patch.object(abr_v41, "persist_backup"), patch.object(
            abr_v41, "read_storage_backup_info", return_value=locked
        ) as asked:
            failures = abr_v41.apply_retention_to_chain(JOBS["job-1"], vm_id="vm-1", repository_id="repo-1", report=report)

        remove.assert_not_called()
        self.assertEqual(failures, [])
        self.assertEqual(report["removed"], [])
        self.assertEqual(report["held"], [{"point_id": "inc-1", "until": "2999-01-01T00:00:00+00:00"}])
        self.assertEqual([call.args[1:3] for call in asked.call_args_list], [("prefix/vm/inc-1", "01-vm.vhdx")])
        self.assertEqual(report["kept_for_chain"], ["full-1"])
        self.assertIn("full-1", BACKUPS)
        self.assertIn("inc-1", BACKUPS)


class PointLockVisibilityTests(unittest.TestCase):
    def test_both_point_listings_show_when_the_lock_ends(self) -> None:
        point = abr_v41.normalize_backup_point(
            {
                "id": "point-1",
                "job_id": "job-1",
                "vm_id": "vm-1",
                "repository_id": "repo-1",
                "hypervisor_kind": "hyper-v",
                "status": "success",
                "full_backup": True,
                "execution_type": "ActiveFull",
                "created_at": "2026-01-01T00:00:00Z",
                "immutability_expires_at": "2026-01-08T00:00:00+00:00",
            }
        )

        chain = abr_v41.backup_chain_to_dto([point])

        self.assertEqual(chain["restore_points"][0]["immutability_expires_at"], "2026-01-08T00:00:00+00:00")
        self.assertEqual(abr_v41.restore_point_to_dto(point)["immutability_expires_at"], "2026-01-08T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
