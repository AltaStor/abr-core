# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from python.srv.core.mongo_db_wrapper import (
    BACKUPS,
    HYPERVISORS,
    JOBS,
    REPOSITORIES,
    RESOURCES_BY_HV,
    TASKS,
)
from python.srv.core.storage_tool import (
    StorageToolError,
    disk_tool_storage_args,
    extract_s3_config,
    s3_endpoint_connect_error,
    s3_endpoint_connect_timeout,
)
from python.srv.core.subprocess_util import CompletedProcess
from python.srv.services import abr_v41
from python.srv.services.api_errors import ApiError


def _disk_tool_stub(*backup_names: str):
    """Stand in for abr-vmdk-tool, echoing the real "list" output shape."""
    if backup_names:
        listing = "Backups in 'repo':\n" + "".join(f"  {name}\n" for name in backup_names)
    else:
        listing = "No backups found in 'repo'\n"

    def _side_effect(command: list[str], **kwargs: object) -> CompletedProcess:
        if command and command[0] == "list":
            return CompletedProcess(args=command, returncode=0, stdout=listing, stderr="")
        if command and command[0] == "info":
            document = '{"command": "info", "status": "ok", "immutability_supported": "compliance", "immutability": "none", "retention_days": 0}'
            return CompletedProcess(args=command, returncode=0, stdout=document, stderr="")
        return CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    return _side_effect


class S3RepositorySupportTests(unittest.TestCase):
    def setUp(self) -> None:
        BACKUPS.clear()
        HYPERVISORS.clear()
        JOBS.clear()
        REPOSITORIES.clear()
        RESOURCES_BY_HV.clear()
        TASKS.clear()
        self._repo_root = tempfile.mkdtemp(prefix="abr-s3-test-repos-")

    def tearDown(self) -> None:
        BACKUPS.clear()
        HYPERVISORS.clear()
        JOBS.clear()
        REPOSITORIES.clear()
        RESOURCES_BY_HV.clear()
        TASKS.clear()
        shutil.rmtree(self._repo_root, ignore_errors=True)

    def _local_repo_path(self, name: str) -> str:
        path = Path(self._repo_root) / name
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.services.abr_v41.s3_bucket_is_reachable", return_value=True)
    @patch("python.srv.services.abr_v41.probe_repository_access")
    def test_create_s3_repository_masks_secrets_in_response(self, _probe, _bucket, _connect) -> None:
        repository = abr_v41.create_repository_record(
            {
                "name": "ObjectRepo",
                "type": "s3",
                "s3": {
                    "bucket": "altastor-backups",
                    "prefix": "lab",
                    "region": "us-east-1",
                    "endpoint_url": "http://minio.local:9000",
                    "access_key": "MINIOACCESS",
                    "secret_key": "super-secret-key",
                    "path_style": True,
                    "verify_tls": False,
                },
            }
        )

        self.assertEqual(repository["type"], "s3")
        self.assertEqual(repository["path"], "s3://altastor-backups/lab")
        self.assertEqual(repository["s3"]["bucket"], "altastor-backups")
        self.assertEqual(repository["s3"]["access_key_masked"], "MINI*****SS")
        self.assertTrue(repository["s3"]["has_secret_key"])
        self.assertNotIn("secret_key", repository["s3"])
        self.assertEqual(
            repository["metadata"]["s3"]["access_key_masked"], "MINI*****SS"
        )
        self.assertTrue(repository["metadata"]["s3"]["has_secret_key"])
        self.assertNotIn("access_key", repository["metadata"]["s3"])
        self.assertNotIn("secret_key", repository["metadata"]["s3"])

        stored = REPOSITORIES[repository["id"]]
        self.assertEqual(extract_s3_config(stored)["secret_key"], "super-secret-key")

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.services.abr_v41.s3_bucket_is_reachable", return_value=True)
    @patch("python.srv.services.abr_v41.probe_repository_access")
    def test_resolve_s3_repository_keeps_stored_credentials_while_masking_response(
        self, _probe, _bucket, _connect
    ) -> None:
        repository = abr_v41.create_repository_record(
            {
                "name": "ObjectRepo",
                "type": "s3",
                "s3": {
                    "bucket": "altastor-backups",
                    "prefix": "lab",
                    "region": "us-east-1",
                    "endpoint_url": "http://minio.local:9000",
                    "access_key": "MINIOACCESS",
                    "secret_key": "super-secret-key",
                    "path_style": True,
                    "verify_tls": False,
                },
            }
        )

        resolved = abr_v41.resolve_repository(record_id=repository["id"])

        self.assertEqual(resolved["s3"]["access_key_masked"], "MINI*****SS")
        self.assertTrue(resolved["s3"]["has_secret_key"])
        self.assertNotIn("access_key", resolved["metadata"]["s3"])
        self.assertNotIn("secret_key", resolved["metadata"]["s3"])
        self.assertEqual(
            extract_s3_config(REPOSITORIES[repository["id"]])["secret_key"],
            "super-secret-key",
        )

    def test_create_smb_repository_masks_password_in_response(self) -> None:
        repository = abr_v41.create_repository_record(
            {
                "name": "probe-smb",
                "type": "smb",
                "server": "10.255.255.9",
                "share": "backups",
                "username": "probe-user",
                "password": "Canary-Password-9f3b21",
            }
        )

        self.assertEqual(repository["metadata"]["password"], "********")
        self.assertNotIn("Canary-Password-9f3b21", str(repository))

        stored = REPOSITORIES[repository["id"]]
        self.assertEqual(stored["metadata"]["password"], "Canary-Password-9f3b21")

    def test_list_and_resolve_smb_repository_also_mask_the_password(self) -> None:
        created = abr_v41.create_repository_record(
            {
                "name": "probe-smb-2",
                "type": "smb",
                "server": "10.255.255.9",
                "share": "backups",
                "username": "probe-user",
                "password": "Canary-Password-2",
            }
        )

        listed = abr_v41.list_repository_records()
        resolved = abr_v41.resolve_repository(record_id=created["id"])

        for record in (listed[0], resolved):
            self.assertEqual(record["metadata"]["password"], "********")
            self.assertNotIn("Canary-Password-2", str(record))

    def test_local_repository_without_a_password_is_unaffected(self) -> None:
        repository = abr_v41.create_repository_record(
            {"name": "probe-local", "type": "local", "path": self._local_repo_path("probe-local")}
        )

        self.assertIsNone(repository["metadata"].get("password"))

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.services.abr_v41.s3_bucket_is_reachable", return_value=True)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_verify_s3_repository_uses_native_disk_tool(
        self, run_disk_tool_mock: object, _bucket: object, _connect_probe: object
    ) -> None:
        repository = abr_v41.create_repository_record(
            {
                "name": "ObjectRepo",
                "type": "s3",
                "s3": {
                    "bucket": "altastor-backups",
                    "prefix": "lab",
                    "region": "us-east-1",
                    "endpoint_url": "http://minio.local:9000",
                    "path_style": True,
                    "verify_tls": False,
                },
            }
        )

        run_disk_tool_mock.reset_mock()
        payload = abr_v41.verify_repository(repository["id"])

        self.assertEqual(payload["status"], "success")
        # There is no single validate command any more: connectivity is proven by
        # round-tripping a probe backup through the bucket.
        commands = [call.args[0] for call in run_disk_tool_mock.call_args_list]
        self.assertEqual([command[0] for command in commands], ["backup", "verify", "remove"])
        for command in commands:
            self.assertEqual(command[command.index("--mode") + 1], "s3")
            self.assertEqual(command[command.index("--folder") + 1], "altastor-backups")
        # Verifying a repository provisions the bucket when it is missing...
        self.assertIn("--create-folder", commands[0])
        # ...and the probe must not survive the check.
        self.assertIn("--force", commands[2])
        self.assertEqual(commands[0][commands[0].index("--name") + 1], commands[2][commands[2].index("--name") + 1])

    @patch("python.srv.services.abr_v41.s3_bucket_is_reachable", return_value=True)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_delete_backup_artifacts_from_s3_point_deletes_remote_and_local_cache(
        self, run_disk_tool_mock: object, _bucket: object
    ) -> None:
        repository = abr_v41.create_repository_record(
            {
                "name": "ObjectRepo",
                "type": "s3",
                "s3": {
                    "bucket": "altastor-backups",
                    "prefix": "lab",
                    "region": "us-east-1",
                },
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            backup_root = Path(tmpdir) / "vm-1" / "task-1"
            backup_root.mkdir(parents=True, exist_ok=True)
            manifest_path = backup_root / "manifest.json"
            manifest_path.write_text("{}", encoding="utf-8")

            point = abr_v41.normalize_backup_point(
                {
                    "id": "point-1",
                    "repository_id": repository["id"],
                    "repository_name": repository["name"],
                    "repository_type": "s3",
                    "vm_id": "vm-1",
                    "vm_name": "VM-1",
                    "execution_type": "ActiveFull",
                    "created_at": "2026-06-21T12:00:00+00:00",
                    "artifact": {
                        "backup_root": str(backup_root),
                        "manifest_path": str(manifest_path),
                        "disks": [
                            {
                                "index": 1,
                                "storage_mode": "s3",
                                "abr_path": "vm-1/task-1",
                                "abr_backup_name": "01-disk-1.vmdk",
                            }
                        ],
                    },
                }
            )

            run_disk_tool_mock.reset_mock()
            abr_v41._delete_backup_artifacts_from_point(point)

            self.assertEqual(run_disk_tool_mock.call_count, 2)
            commands = [call.args[0] for call in run_disk_tool_mock.call_args_list]
            self.assertTrue(all(command[0] == "remove" for command in commands))
            # remove prompts on stdin without it, and the subprocess has no stdin.
            self.assertTrue(all("--force" in command for command in commands))
            disk_command = next(command for command in commands if "01-disk-1.vmdk" in command)
            manifest_command = next(command for command in commands if "manifest" in command)
            self.assertIsNot(disk_command, manifest_command)
            self.assertFalse(backup_root.exists())
            self.assertFalse(manifest_path.exists())

    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_delete_backup_artifacts_from_local_point_also_calls_the_storage_tool(
        self, run_disk_tool_mock: object
    ) -> None:
        repository_path = self._local_repo_path("altastor-remove")
        repository = abr_v41.create_repository_record(
            {"name": "LocalRepoRemove", "type": "local", "path": repository_path}
        )

        backup_root = Path(repository_path) / "vm-1" / "task-1"
        backup_root.mkdir(parents=True, exist_ok=True)
        manifest_path = backup_root / "manifest.json"
        manifest_path.write_text("{}", encoding="utf-8")

        point = abr_v41.normalize_backup_point(
            {
                "id": "point-1",
                "repository_id": repository["id"],
                "repository_name": repository["name"],
                "repository_type": "local",
                "vm_id": "vm-1",
                "vm_name": "VM-1",
                "execution_type": "ActiveFull",
                "created_at": "2026-06-21T12:00:00+00:00",
                "artifact": {
                    "backup_root": str(backup_root),
                    "manifest_path": str(manifest_path),
                    "disks": [
                        {
                            "index": 1,
                            "storage_mode": "local",
                            "abr_path": str(backup_root),
                            "abr_backup_name": "01-disk-1.vmdk",
                        }
                    ],
                },
            }
        )

        run_disk_tool_mock.reset_mock()
        abr_v41._delete_backup_artifacts_from_point(point)

        # Previously local disks skipped the backup tool entirely and
        # relied only on shutil.rmtree; now the .abr artifact removal
        # goes through remove too, in addition to the folder cleanup.
        self.assertEqual(run_disk_tool_mock.call_count, 2)
        commands = [call.args[0] for call in run_disk_tool_mock.call_args_list]
        self.assertTrue(all(command[0] == "remove" for command in commands))
        self.assertTrue(any("01-disk-1.vmdk" in command for command in commands))
        self.assertTrue(any("manifest" in command for command in commands))
        self.assertFalse(backup_root.exists())
        self.assertFalse(manifest_path.exists())

    def test_disk_tool_storage_args_create_folder_flag(self) -> None:
        repository = {
            "type": "s3",
            "s3": {
                "bucket": "altastor-backups",
                "prefix": "lab",
                "region": "us-east-1",
            },
        }

        without_flag = disk_tool_storage_args(repository, abr_path="vm-1/task-1")
        with_flag = disk_tool_storage_args(
            repository, abr_path="vm-1/task-1", create_folder=True
        )

        self.assertNotIn("--create-folder", without_flag)
        self.assertIn("--create-folder", with_flag)
        # The flag does not consume a value and stays bundled with the S3 args.
        self.assertEqual(
            with_flag[with_flag.index("--folder") + 1], "altastor-backups"
        )

    def test_restore_point_dto_includes_repository_type(self) -> None:
        point = abr_v41.normalize_backup_point(
            {
                "id": "point-1",
                "chain_id": "backup::job-1::vm-1::repo-1",
                "job_id": "job-1",
                "job_name": "Daily",
                "vm_id": "vm-1",
                "vm_name": "VM-1",
                "inventory_id": "hv-1",
                "inventory_name": "vCenter",
                "repository_id": "repo-1",
                "repository_name": "ObjectRepo",
                "repository_type": "s3",
                "execution_type": "ActiveFull",
                "created_at": "2026-06-21T12:00:00+00:00",
                "status": "available",
            }
        )

        payload = abr_v41.restore_point_to_dto(point)

        self.assertEqual(payload["repository_type"], "s3")
        self.assertEqual(payload["repository"]["type"], "s3")

    def _persist_point_with_disks(
        self,
        *,
        repository: dict[str, object],
        disks: list[dict[str, object]],
        **overrides: object,
    ) -> dict[str, object]:
        point = abr_v41.normalize_backup_point(
            {
                "id": "point-1",
                "job_id": "job-1",
                "vm_id": "vm-1",
                "vm_name": "VM-1",
                "repository_id": repository["id"],
                "repository_name": repository["name"],
                "repository_type": repository["type"],
                "execution_type": "ActiveFull",
                "created_at": "2026-06-21T12:00:00+00:00",
                "artifact": {"disks": disks},
                **overrides,
            }
        )
        abr_v41.persist_backup(point)
        return point

    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_verify_backup_chain_calls_verify_for_each_abr_backed_disk(
        self, run_disk_tool_mock: object
    ) -> None:
        run_disk_tool_mock.side_effect = _disk_tool_stub("01-disk-1.vmdk", "02-disk-2.vmdk")
        repository = abr_v41.create_repository_record(
            {"name": "LocalRepo", "type": "local", "path": self._local_repo_path("altastor")}
        )

        point = self._persist_point_with_disks(
            repository=repository,
            disks=[
                {
                    "disk_key": "disk-1",
                    "storage_mode": "local",
                    "abr_path": "vm-1/task-1",
                    "abr_backup_name": "01-disk-1.vmdk",
                },
                {
                    "disk_key": "disk-2",
                    "storage_mode": "local",
                    "abr_path": "vm-1/task-1",
                    "abr_backup_name": "02-disk-2.vmdk",
                },
            ],
        )

        payload = abr_v41.verify_backup_chain(point["chain_id"])

        self.assertEqual(payload["result"], "valid")
        self.assertEqual(payload["checked_artifacts"], 2)
        self.assertEqual(payload["errors"], [])
        # One list existence pre-check plus one verify per disk.
        self.assertEqual(run_disk_tool_mock.call_count, 4)

        verify_commands = [
            call.args[0] for call in run_disk_tool_mock.call_args_list if call.args[0][0] == "verify"
        ]
        list_commands = [
            call.args[0] for call in run_disk_tool_mock.call_args_list if call.args[0][0] == "list"
        ]
        self.assertEqual(
            {command[command.index("--name") + 1] for command in verify_commands},
            {"01-disk-1.vmdk", "02-disk-2.vmdk"},
        )
        self.assertEqual(len(list_commands), 2)
        for command in verify_commands + list_commands:
            self.assertIn("--folder", command)

    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_verify_backup_chain_reports_disk_tool_failure(
        self, run_disk_tool_mock: object
    ) -> None:
        run_disk_tool_mock.side_effect = StorageToolError(
            "checksum mismatch on range 4"
        )

        repository = abr_v41.create_repository_record(
            {"name": "LocalRepo2", "type": "local", "path": self._local_repo_path("altastor2")}
        )

        point = self._persist_point_with_disks(
            repository=repository,
            disks=[
                {
                    "disk_key": "disk-1",
                    "storage_mode": "local",
                    "abr_path": "vm-1/task-1",
                    "abr_backup_name": "01-disk-1.vmdk",
                }
            ],
        )

        payload = abr_v41.verify_backup_chain(point["chain_id"])

        self.assertEqual(payload["result"], "invalid")
        self.assertEqual(len(payload["errors"]), 1)
        self.assertEqual(payload["errors"][0]["disk_key"], "disk-1")
        self.assertIn("checksum mismatch", payload["errors"][0]["message"])

    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_verify_backup_chain_falls_back_to_existence_check_for_legacy_disk(
        self, run_disk_tool_mock: object
    ) -> None:
        repository = abr_v41.create_repository_record(
            {"name": "LegacyRepo", "type": "local", "path": self._local_repo_path("legacy")}
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            disk_path = Path(tmpdir) / "01-disk-1.vmdk"
            disk_path.write_bytes(b"fake-disk-payload")
            point = self._persist_point_with_disks(
                repository=repository,
                disks=[{"disk_key": "disk-1", "destination_path": str(disk_path)}],
            )

            payload = abr_v41.verify_backup_chain(point["chain_id"])

        self.assertEqual(payload["result"], "valid")
        self.assertEqual(payload["checked_artifacts"], 1)
        run_disk_tool_mock.assert_not_called()

    def test_verify_backup_chain_flags_missing_legacy_disk_file(self) -> None:
        repository = abr_v41.create_repository_record(
            {"name": "LegacyRepo2", "type": "local", "path": self._local_repo_path("legacy2")}
        )
        point = self._persist_point_with_disks(
            repository=repository,
            disks=[
                {
                    "disk_key": "disk-1",
                    "destination_path": "/no/such/path/01-disk-1.vmdk",
                }
            ],
        )

        payload = abr_v41.verify_backup_chain(point["chain_id"])

        self.assertEqual(payload["result"], "invalid")
        self.assertIn("missing", payload["errors"][0]["message"])

    def test_verify_backup_chain_unknown_id_raises_404(self) -> None:
        with self.assertRaises(ApiError) as ctx:
            abr_v41.verify_backup_chain("backup::does-not-exist")
        self.assertEqual(ctx.exception.status_code, 404)

    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_verify_backup_chain_still_flags_incremental_without_parent(
        self, run_disk_tool_mock: object
    ) -> None:
        repository = abr_v41.create_repository_record(
            {"name": "LocalRepo3", "type": "local", "path": self._local_repo_path("altastor3")}
        )

        point = self._persist_point_with_disks(
            repository=repository,
            disks=[],
            execution_type="Incremental",
        )

        payload = abr_v41.verify_backup_chain(point["chain_id"])

        self.assertEqual(payload["result"], "warning")
        self.assertEqual(len(payload["warnings"]), 1)
        self.assertEqual(payload["checked_artifacts"], 0)

        run_disk_tool_mock.assert_not_called()

    def test_repo_verify_local_reports_ok_with_no_backups(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repository = abr_v41.create_repository_record(
                {"name": "EmptyRepo", "type": "local", "path": tmpdir}
            )

            payload = abr_v41.verify_repository(repository["id"])

        self.assertEqual(payload["status"], "success")
        block_check = next(
            check for check in payload["checks"] if check["name"] == "block_consistency"
        )
        self.assertEqual(block_check["status"], "skipped")

    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_repo_verify_local_checks_every_point_a_restore_depends_on(
        self, run_disk_tool_mock: object
    ) -> None:
        run_disk_tool_mock.side_effect = _disk_tool_stub("old-full.vmdk", "new-inc.vmdk")
        with tempfile.TemporaryDirectory() as tmpdir:
            repository = abr_v41.create_repository_record(
                {"name": "LocalRepo4", "type": "local", "path": tmpdir}
            )

            older = abr_v41.normalize_backup_point(
                {
                    "id": "point-old",
                    "job_id": "job-1",
                    "vm_id": "vm-1",
                    "vm_name": "VM-1",
                    "repository_id": repository["id"],
                    "repository_name": repository["name"],
                    "repository_type": repository["type"],
                    "execution_type": "ActiveFull",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "artifact": {
                        "disks": [
                            {
                                "disk_key": "disk-1",
                                "abr_path": "vm-1/task-1",
                                "abr_backup_name": "old-full.vmdk",
                            }
                        ]
                    },
                }
            )
            newer = abr_v41.normalize_backup_point(
                {
                    "id": "point-new",
                    "job_id": "job-1",
                    "vm_id": "vm-1",
                    "vm_name": "VM-1",
                    "repository_id": repository["id"],
                    "repository_name": repository["name"],
                    "repository_type": repository["type"],
                    "execution_type": "Incremental",
                    "parent_point_id": "point-old",
                    "created_at": "2026-01-02T00:00:00+00:00",
                    "artifact": {
                        "disks": [
                            {
                                "disk_key": "disk-1",
                                "abr_path": "vm-1/task-1",
                                "abr_backup_name": "new-inc.vmdk",
                            }
                        ]
                    },
                }
            )
            abr_v41.persist_backup(older)
            abr_v41.persist_backup(newer)

            payload = abr_v41.verify_repository(repository["id"])

        self.assertEqual(payload["status"], "success")

        verified = sorted(
            call.args[0][2] for call in run_disk_tool_mock.call_args_list if call.args[0][0] == "verify"
        )
        self.assertEqual(verified, ["new-inc.vmdk", "old-full.vmdk"])

        block_check = next(
            check for check in payload["checks"] if check["name"] == "block_consistency"
        )
        self.assertEqual(block_check["status"], "ok")
        self.assertEqual(
            block_check["message"], "Verified 2 backup artifact(s) of 2 restore point(s) across 1 chain(s)"
        )

    def _chain_point(self, repository: dict, point_id: str, created_at: str, backup_name: str, *, parent: str | None = None) -> None:
        abr_v41.persist_backup(
            abr_v41.normalize_backup_point(
                {
                    "id": point_id,
                    "job_id": "job-1",
                    "vm_id": "vm-1",
                    "vm_name": "VM-1",
                    "repository_id": repository["id"],
                    "repository_name": repository["name"],
                    "repository_type": repository["type"],
                    "execution_type": "Incremental" if parent else "ActiveFull",
                    **({"parent_point_id": parent} if parent else {}),
                    "created_at": created_at,
                    "artifact": {"disks": [{"disk_key": "disk-1", "abr_path": "vm-1/task-1", "abr_backup_name": backup_name}]},
                }
            )
        )

    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_repo_verify_fails_on_a_corrupted_full_beneath_the_newest_point(self, run_disk_tool_mock: object) -> None:
        listing = _disk_tool_stub("full.vmdk", "inc-1.vmdk", "inc-2.vmdk")

        def tool(command: list[str], **kwargs: object) -> CompletedProcess:
            if command[0] == "verify" and command[2] == "full.vmdk":
                raise StorageToolError("CRC32C mismatch for restored chunk 2! full.vmdk is CORRUPT")
            return listing(command, **kwargs)

        run_disk_tool_mock.side_effect = tool
        with tempfile.TemporaryDirectory() as tmpdir:
            repository = abr_v41.create_repository_record({"name": "LocalRepoB", "type": "local", "path": tmpdir})
            self._chain_point(repository, "b-full", "2026-01-01T00:00:00+00:00", "full.vmdk")
            self._chain_point(repository, "b-inc-1", "2026-01-02T00:00:00+00:00", "inc-1.vmdk", parent="b-full")
            self._chain_point(repository, "b-inc-2", "2026-01-03T00:00:00+00:00", "inc-2.vmdk", parent="b-inc-1")

            payload = abr_v41.verify_repository(repository["id"])

        self.assertEqual(payload["status"], "error")
        block_check = next(check for check in payload["checks"] if check["name"] == "block_consistency")
        self.assertEqual(block_check["status"], "failed")
        self.assertEqual(
            block_check["message"], "1 of 3 backup artifact(s) of 3 restore point(s) across 1 chain(s) failed verification"
        )
        self.assertEqual([error["point_id"] for error in block_check["errors"]], ["b-full"])
        self.assertTrue(block_check["errors"][0]["chain_id"].startswith("backup::job-1::vm-1::"))
        self.assertEqual(payload["last_verify"]["status"], "error")

    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_registering_or_editing_a_repository_still_reads_only_the_newest_points_and_says_so(
        self, run_disk_tool_mock: object
    ) -> None:
        run_disk_tool_mock.side_effect = _disk_tool_stub("full.vmdk", "inc-1.vmdk")
        with tempfile.TemporaryDirectory() as tmpdir:
            repository = abr_v41.create_repository_record({"name": "LocalRepoC", "type": "local", "path": tmpdir})
            self._chain_point(repository, "c-full", "2026-01-01T00:00:00+00:00", "full.vmdk")
            self._chain_point(repository, "c-inc-1", "2026-01-02T00:00:00+00:00", "inc-1.vmdk", parent="c-full")

            result = abr_v41._repository_registration_checks(dict(REPOSITORIES[repository["id"]]))

        verified = [call.args[0][2] for call in run_disk_tool_mock.call_args_list if call.args[0][0] == "verify"]
        self.assertEqual(verified, ["inc-1.vmdk"])
        block_check = next(check for check in result["checks"] if check["name"] == "block_consistency")
        self.assertEqual(
            block_check["message"], "Verified 1 backup artifact(s) of the newest restore point of each of 1 chain(s)"
        )

    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_repo_verify_reads_only_the_points_this_repository_holds(self, run_disk_tool_mock: object) -> None:
        run_disk_tool_mock.side_effect = _disk_tool_stub("mine.vmdk", "theirs.vmdk")
        with tempfile.TemporaryDirectory() as tmpdir, tempfile.TemporaryDirectory() as other_dir:
            mine = abr_v41.create_repository_record({"name": "LocalRepoD", "type": "local", "path": tmpdir})
            other = abr_v41.create_repository_record({"name": "LocalRepoE", "type": "local", "path": other_dir})
            self._chain_point(mine, "d-full", "2026-01-01T00:00:00+00:00", "mine.vmdk")
            self._chain_point(other, "e-full", "2026-01-01T00:00:00+00:00", "theirs.vmdk")

            abr_v41.verify_repository(mine["id"])

        verified = [call.args[0][2] for call in run_disk_tool_mock.call_args_list if call.args[0][0] == "verify"]
        self.assertEqual(verified, ["mine.vmdk"])

    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_repo_verify_local_fails_when_backup_corrupted(
        self, run_disk_tool_mock: object
    ) -> None:
        run_disk_tool_mock.side_effect = StorageToolError("Header CRC32C mismatch")
        with tempfile.TemporaryDirectory() as tmpdir:
            repository = abr_v41.create_repository_record(
                {"name": "LocalRepo5", "type": "local", "path": tmpdir}
            )
            self._persist_point_with_disks(
                repository=repository,
                disks=[
                    {
                        "disk_key": "disk-1",
                        "abr_path": "vm-1/task-1",
                        "abr_backup_name": "corrupt.vmdk",
                    }
                ],
            )

            payload = abr_v41.verify_repository(repository["id"])

        self.assertEqual(payload["status"], "error")
        block_check = next(
            check for check in payload["checks"] if check["name"] == "block_consistency"
        )
        self.assertEqual(block_check["status"], "failed")
        self.assertEqual(len(block_check["errors"]), 1)
        self.assertIn("Header CRC32C mismatch", block_check["errors"][0]["message"])

    def test_create_local_repository_refuses_a_path_it_cannot_stat_instead_of_crashing(
        self,
    ) -> None:
        path = self._local_repo_path("probe-unstatable")
        with patch.object(Path, "exists", side_effect=PermissionError("denied")), self.assertRaises(
            ApiError
        ) as ctx:
            abr_v41.create_repository_record({"name": "ProbeBad", "type": "local", "path": path})

        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("not accessible", ctx.exception.message)
        self.assertIn(path, ctx.exception.message)

    def test_create_local_repository_refuses_a_path_whose_directory_check_is_denied(
        self,
    ) -> None:
        path = self._local_repo_path("probe-undirable")
        with patch.object(Path, "is_dir", side_effect=PermissionError("denied")), self.assertRaises(
            ApiError
        ) as ctx:
            abr_v41.create_repository_record({"name": "ProbeBad2", "type": "local", "path": path})

        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("not accessible", ctx.exception.message)

    def test_create_local_repository_still_refuses_a_genuinely_missing_path(self) -> None:
        missing = str(Path(self._repo_root) / "does-not-exist-at-all")
        with self.assertRaises(ApiError) as ctx:
            abr_v41.create_repository_record({"name": "ProbeMissing", "type": "local", "path": missing})

        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("does not exist", ctx.exception.message)
        self.assertNotIn("not accessible", ctx.exception.message)

    @unittest.skipIf(os.geteuid() == 0, "root bypasses directory permission checks")
    def test_create_local_repository_refuses_a_real_permission_denied_symlink(self) -> None:
        closed_dir = Path(self._repo_root) / "closed"
        (closed_dir / "target").mkdir(parents=True)
        link = Path(self._repo_root) / "link-through-closed-dir"
        link.symlink_to(closed_dir / "target")
        closed_dir.chmod(0o000)
        try:
            with self.assertRaises(ApiError) as ctx:
                abr_v41.create_repository_record({"name": "ProbeRealDenied", "type": "local", "path": str(link)})
        finally:
            closed_dir.chmod(0o755)

        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("not accessible", ctx.exception.message)

    def test_repo_verify_local_reports_a_clean_error_when_path_is_not_accessible(self) -> None:
        with patch.object(Path, "exists", side_effect=PermissionError("denied")):
            result = abr_v41._verify_local_repository({"path": self._local_repo_path("probe-verify-denied")})

        self.assertEqual(result["status"], "error")
        self.assertIn("not accessible", result["message"])

    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_repo_verify_s3_also_checks_backup_health(
        self, run_disk_tool_mock: object
    ) -> None:
        run_disk_tool_mock.side_effect = _disk_tool_stub("01-disk-1.vmdk")
        repository = abr_v41.create_repository_record(
            {
                "name": "S3RepoHealth",
                "type": "s3",
                "s3": {
                    "bucket": "altastor-backups",
                    "prefix": "lab",
                    "region": "us-east-1",
                },
            }
        )
        self._persist_point_with_disks(
            repository=repository,
            disks=[
                {
                    "disk_key": "disk-1",
                    "storage_mode": "s3",
                    "abr_path": "vm-1/task-1",
                    "abr_backup_name": "01-disk-1.vmdk",
                }
            ],
        )

        run_disk_tool_mock.reset_mock()
        payload = abr_v41.verify_repository(repository["id"])

        self.assertEqual(payload["status"], "success")
        # The connectivity probe (backup/verify/remove), then the existence
        # pre-check and the block-level verify of the stored artifact.
        commands = [call.args[0][0] for call in run_disk_tool_mock.call_args_list]
        self.assertEqual(commands, ["backup", "verify", "remove", "list", "verify"])
        block_check = next(
            check for check in payload["checks"] if check["name"] == "block_consistency"
        )
        self.assertEqual(block_check["status"], "ok")

    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_verify_disk_artifact_fails_fast_when_list_omits_the_backup_name(
        self, run_disk_tool_mock: object
    ) -> None:
        run_disk_tool_mock.side_effect = _disk_tool_stub("some-other-backup.vmdk")
        repository = abr_v41.create_repository_record(
            {"name": "LocalRepoMissing", "type": "local", "path": self._local_repo_path("altastor-missing")}
        )

        result = abr_v41._verify_disk_artifact(
            {"repository_id": repository["id"]},
            {"disk_key": "disk-1", "abr_path": "vm-1/task-1", "abr_backup_name": "01-disk-1.vmdk"},
        )

        self.assertEqual(result["status"], "failed")
        self.assertIn("not found", result["message"])
        run_disk_tool_mock.assert_called_once()
        self.assertEqual(run_disk_tool_mock.call_args.args[0][0], "list")

    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_load_restore_manifest_record_recovers_via_meta_load_when_local_copy_is_missing(
        self, run_disk_tool_mock: object
    ) -> None:
        repository = abr_v41.create_repository_record(
            {"name": "LocalRepoHydrate", "type": "local", "path": self._local_repo_path("altastor-hydrate")}
        )
        manifest_path = Path(repository["path"]) / "vm-1" / "task-1" / "manifest.json"

        def _fake_run_disk_tool(command, **kwargs):
            self.assertEqual(command[0], "meta_load")
            self.assertEqual(command[command.index("--target") + 1], str(manifest_path))
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text('{"compression_level": 3}', encoding="utf-8")
            return CompletedProcess(args=command, returncode=0, stdout="", stderr="")

        run_disk_tool_mock.side_effect = _fake_run_disk_tool

        point = {
            "id": "point-1",
            "repository_id": repository["id"],
            "artifact": {
                "manifest_path": str(manifest_path),
                "disks": [{"abr_path": "vm-1/task-1", "abr_backup_name": "01-disk.vmdk"}],
            },
        }

        payload = abr_v41._load_restore_manifest_record(point)

        run_disk_tool_mock.assert_called_once()
        self.assertEqual(payload, {"compression_level": 3})

    def test_load_restore_manifest_record_stays_empty_when_no_repository_is_known(self) -> None:
        point = {
            "id": "point-1",
            "artifact": {"manifest_path": "/no/such/manifest.json", "disks": []},
        }
        self.assertEqual(abr_v41._load_restore_manifest_record(point), {})


class S3EndpointReachabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        REPOSITORIES.clear()

    def tearDown(self) -> None:
        REPOSITORIES.clear()

    @staticmethod
    def _listening_port() -> tuple[socket.socket, int]:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        return listener, listener.getsockname()[1]

    @staticmethod
    def _closed_port() -> int:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        return port

    def test_connect_error_is_none_for_a_listening_endpoint(self) -> None:
        listener, port = self._listening_port()
        self.addCleanup(listener.close)
        self.assertIsNone(s3_endpoint_connect_error(f"http://127.0.0.1:{port}"))

    def test_connect_error_reports_a_refused_port(self) -> None:
        error = s3_endpoint_connect_error(f"http://127.0.0.1:{self._closed_port()}")
        self.assertIsNotNone(error)
        self.assertIn("refused", str(error).lower())

    def test_connect_error_reports_an_unresolvable_host(self) -> None:
        error = s3_endpoint_connect_error("http://minio.local:9000")
        self.assertIsNotNone(error)
        self.assertIn("cannot resolve", str(error))

    def test_connect_error_rejects_a_malformed_port_instead_of_raising(self) -> None:
        self.assertEqual(
            s3_endpoint_connect_error("http://example.com:notaport"),
            "endpoint URL has an invalid port",
        )

    def test_the_shipped_budget_keeps_a_dead_endpoint_close_to_the_live_failures(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ALTASTOR_S3_CONNECT_TIMEOUT_SECONDS", None)
            self.assertLessEqual(s3_endpoint_connect_timeout(), 2)

    def test_a_dead_endpoint_is_refused_within_the_budget(self) -> None:
        started = time.monotonic()
        error = s3_endpoint_connect_error("http://192.0.2.1:3900")
        elapsed = time.monotonic() - started

        self.assertIsNotNone(error)
        self.assertLess(elapsed, s3_endpoint_connect_timeout() + 1.0)

    def test_connect_budget_covers_the_whole_call_not_each_address(self) -> None:
        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (f"192.0.2.{n}", 3900))
            for n in (1, 2, 3)
        ]
        budget = 4
        clock = [1000.0]
        granted: list[float] = []

        def _hang(self_socket: object, _address: object) -> None:
            timeout = self_socket.gettimeout()
            granted.append(timeout)
            clock[0] += timeout
            raise socket.timeout("timed out")

        with patch("python.srv.core.storage_tool.socket.getaddrinfo", return_value=addresses), \
                patch("python.srv.core.storage_tool.socket.socket.connect", _hang), \
                patch("python.srv.core.storage_tool.time.monotonic", lambda: clock[0]), \
                patch(
                    "python.srv.core.storage_tool.s3_endpoint_connect_timeout",
                    return_value=budget,
                ):
            error = s3_endpoint_connect_error("http://dead.example:3900")

        self.assertIsNotNone(error)
        self.assertEqual(clock[0] - 1000.0, float(budget))
        self.assertEqual(granted, [float(budget)])

    @patch("python.srv.services.abr_v41.s3_bucket_is_reachable", return_value=True)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_verify_fails_fast_without_handing_a_dead_endpoint_to_the_mover(
        self, run_disk_tool_mock: object, _bucket: object
    ) -> None:
        with patch.object(abr_v41, "s3_endpoint_connect_error", return_value=None):
            repository = abr_v41.create_repository_record(
            {
                "name": "DeadObjectRepo",
                "type": "s3",
                "s3": {
                    "bucket": "example-bucket",
                    "region": "garage",
                    "endpoint_url": f"http://127.0.0.1:{self._closed_port()}",
                    "access_key": "DUMMY",
                    "secret_key": "DUMMY",
                    "path_style": True,
                    "verify_tls": False,
                },
            }
            )
        run_disk_tool_mock.reset_mock()

        payload = abr_v41.verify_repository(repository["id"])

        self.assertEqual(payload["status"], "error")
        self.assertIn("not reachable", payload["message"])
        checks = {check["name"]: check["status"] for check in payload["checks"]}
        self.assertEqual(checks["endpoint_reachable"], "failed")
        run_disk_tool_mock.assert_not_called()
        self.assertNotIn("connectivity", checks)

    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_pre_create_connection_test_fails_the_same_way_as_verify(
        self, run_disk_tool_mock: object
    ) -> None:
        payload = abr_v41.test_s3_connection(
            {
                "bucket": "example-bucket",
                "region": "garage",
                "endpoint_url": f"http://127.0.0.1:{self._closed_port()}",
                "access_key": "DUMMY",
                "secret_key": "DUMMY",
                "path_style": True,
                "verify_tls": False,
            }
        )

        self.assertEqual(payload["status"], "error")
        self.assertIn("not reachable", payload["message"])
        run_disk_tool_mock.assert_not_called()

    @patch("python.srv.core.storage_tool.run_disk_tool", side_effect=_disk_tool_stub())
    def test_repository_without_an_endpoint_is_not_reachability_checked(
        self, _run_disk_tool_mock: object
    ) -> None:
        repository = abr_v41.create_repository_record(
            {
                "name": "AwsRepo",
                "type": "s3",
                "s3": {"bucket": "altastor-backups", "region": "us-east-1"},
            }
        )

        with patch(
            "python.srv.services.abr_v41.s3_endpoint_connect_error"
        ) as connect_probe:
            payload = abr_v41.verify_repository(repository["id"])

        connect_probe.assert_not_called()
        self.assertEqual(payload["status"], "success")
        self.assertNotIn(
            "endpoint_reachable", {check["name"] for check in payload["checks"]}
        )


class DiskToolTimeoutTests(unittest.TestCase):
    @patch("python.srv.core.storage_tool.run_capped")
    def test_run_disk_tool_wraps_timeout_as_storage_tool_error(
        self, run_capped_mock: object
    ) -> None:
        from python.srv.core.storage_tool import run_disk_tool

        run_capped_mock.side_effect = subprocess.TimeoutExpired(
            cmd=["abr-vmdk-tool.sh"],
            timeout=5,
            output="",
            stderr="stuck reading range 12",
        )
        with tempfile.NamedTemporaryFile() as tool_file:
            with self.assertRaises(StorageToolError) as ctx:
                run_disk_tool(
                    ["verify", "--name", "backup-1"],
                    tool_path=Path(tool_file.name),
                    timeout=5,
                )
        self.assertIn("timed out", str(ctx.exception))


class S3BackupStorageLayoutTests(unittest.TestCase):
    def _abr_path(self, prefix: object) -> str:
        from python.srv.plugins.vcenter.vcenter_backup import _resolve_backup_storage_layout

        repository = {"type": "s3", "id": "repo-1", "s3": {"bucket": "altastor-backups", "prefix": prefix}}
        _, _, abr_path = _resolve_backup_storage_layout(
            task={"id": "task-1"}, vm_name="web01", parameters={}, repository=repository
        )
        return abr_path

    def test_backup_is_stored_under_the_repository_prefix(self) -> None:
        self.assertEqual(self._abr_path("lab/vm-backups"), "lab/vm-backups/web01/task-1")

    def test_repository_without_a_prefix_stores_at_the_bucket_root(self) -> None:
        self.assertEqual(self._abr_path(""), "web01/task-1")
        self.assertEqual(self._abr_path(None), "web01/task-1")

    def test_two_repositories_in_one_bucket_do_not_share_a_location(self) -> None:
        self.assertNotEqual(self._abr_path("team-a"), self._abr_path("team-b"))


if __name__ == "__main__":
    unittest.main()
