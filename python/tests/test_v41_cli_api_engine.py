# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import io
import json
import shutil
import socket
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from contextlib import redirect_stdout
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from python.cli.exit_codes import INVALID_STATE
from python.cli.parsing.parser_builder import AltaStorParserBuilder, RepositoryCommands
from python.srv.core.mongo_db_wrapper import BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS
from python.srv.services import abr_v41
from python.srv.services.api_errors import ApiError
from python.srv.services.inventory_management import EsxHostInClusterError


class _FakeCliResponse:
    def __init__(self, payload: dict[str, object], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.content = b"{}"
        self.headers: dict[str, str] = {}

    def json(self) -> dict[str, object]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class AbrV41CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = AltaStorParserBuilder().build()

    def test_root_command_tree_is_v41_only(self) -> None:
        help_output = self.parser.format_help()
        self.assertIn("{auth,user,repo,inventory,job,session,backup,restore,system}", help_output)
        self.assertNotIn("license", help_output)

    def test_obsolete_commands_are_rejected(self) -> None:
        obsolete = [
            ["job", "schedule"],
            ["job", "gfs"],
            ["job", "run"],
            ["backup", "vm"],
            ["backup", "disk"],
            ["restore", "full"],
            ["session", "logs"],
            ["system", "status"],
        ]
        for argv in obsolete:
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit):
                    self.parser.parse_args(argv)

    def test_required_command_helps_parse(self) -> None:
        commands = [
            ["repo", "add", "--help"],
            ["repo", "list", "--help"],
            ["repo", "info", "--help"],
            ["repo", "edit", "--help"],
            ["repo", "remove", "--help"],
            ["repo", "rescan", "--help"],
            ["repo", "verify", "--help"],
            ["inventory", "add", "--help"],
            ["inventory", "list", "--help"],
            ["inventory", "info", "--help"],
            ["inventory", "edit", "--help"],
            ["inventory", "remove", "--help"],
            ["inventory", "rescan", "--help"],
            ["inventory", "test", "--help"],
            ["job", "create", "--help"],
            ["job", "create-copy", "--help"],
            ["job", "list", "--help"],
            ["job", "info", "--help"],
            ["job", "edit", "--help"],
            ["job", "delete", "--help"],
            ["job", "start", "--help"],
            ["session", "list", "--help"],
            ["session", "info", "--help"],
            ["session", "log", "--help"],
            ["session", "stop", "--help"],
            ["backup", "list", "--help"],
            ["backup", "info", "--help"],
            ["backup", "delete", "--help"],
            ["backup", "verify", "--help"],
            ["backup", "import", "--help"],
            ["restore", "--help"],
            ["restore", "list", "--help"],
            ["restore", "point", "list", "--help"],
            ["restore", "start", "--help"],
            ["system", "version", "--help"],
            ["system", "health", "--help"],
        ]
        for argv in commands:
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as ctx:
                    self.parser.parse_args(argv)
                self.assertEqual(ctx.exception.code, 0)

    def test_job_create_commands_accept_missing_schedule(self) -> None:
        create_args = self.parser.parse_args(
            [
                "job",
                "create",
                "--name",
                "Nightly",
                "--vms",
                "vm-1",
                "--repo",
                "Repo",
                "--retention-days",
                "7",
            ]
        )
        copy_args = self.parser.parse_args(
            [
                "job",
                "create-copy",
                "--name",
                "Archive",
                "--source-job",
                "Nightly",
                "--repo",
                "Repo",
                "--retention-days",
                "14",
            ]
        )

        self.assertIsNone(create_args.schedule)
        self.assertIsNone(copy_args.schedule)

    def test_repo_add_and_edit_accept_optional_description(self) -> None:
        add_args = self.parser.parse_args(
            [
                "repo",
                "add",
                "--name",
                "Archive",
                "--type",
                "smb",
                "--address",
                "//nas/archive",
                "--username",
                "backup",
                "--password",
                "secret",
                "--description",
                "Archive tier",
            ]
        )
        edit_args = self.parser.parse_args(
            [
                "repo",
                "edit",
                "--name",
                "Archive",
                "--description",
                "Updated archive tier",
            ]
        )

        self.assertEqual(add_args.description, "Archive tier")
        self.assertEqual(edit_args.description, "Updated archive tier")


class AbrV41RepositoryCommandTests(unittest.TestCase):
    @patch("python.cli.api.client.requests.request")
    def test_repo_add_command_posts_description_and_remote_address_parts(self, request_mock: object) -> None:
        request_mock.return_value = _FakeCliResponse(
            {
                "id": "repo-1",
                "name": "Archive",
                "type": "smb",
                "path": "//nas/archive",
                "status": "available",
            }
        )
        args = SimpleNamespace(
            base_url="http://example.test",
            output="json",
            name="Archive",
            type="smb",
            path=None,
            address="//nas/archive",
            server=None,
            share=None,
            username="backup",
            password="secret",
            description="Archive tier",
        )

        with redirect_stdout(io.StringIO()):
            exit_code = RepositoryCommands().create(args)

        self.assertEqual(exit_code, 0)
        request_mock.assert_called_once_with(
            "POST",
            "http://example.test/api/v1/repositories",
            json={
                "name": "Archive",
                "type": "smb",
                "path": None,
                "server": "nas",
                "share": "archive",
                "username": "backup",
                "password": "secret",
                "description": "Archive tier",
            },
            params=None,
            timeout=30,
            headers={"X-Client-Type": "cli", "X-ABR-API-Version": "1.0"},
            verify=False,
        )

    @patch("python.cli.api.client.requests.request")
    def test_repo_add_command_posts_s3_payload_with_validation(self, request_mock: object) -> None:
        request_mock.return_value = _FakeCliResponse(
            {
                "id": "repo-1",
                "name": "ObjectRepo",
                "type": "s3",
                "path": "s3://altastor-backups/lab",
                "status": "available",
            }
        )
        args = SimpleNamespace(
            base_url="http://example.test",
            output="json",
            name="ObjectRepo",
            type="s3",
            path=None,
            address=None,
            server=None,
            share=None,
            username=None,
            password=None,
            description="MinIO lab repo",
            bucket="altastor-backups",
            prefix="lab",
            region="us-east-1",
            endpoint_url="http://minio.local:9000",
            access_key="MINIOACCESS",
            secret_key="super-secret-key",
            profile=None,
            path_style=True,
            verify_tls=False,
        )

        with redirect_stdout(io.StringIO()):
            exit_code = RepositoryCommands().create(args)

        self.assertEqual(exit_code, 0)
        request_mock.assert_called_once_with(
            "POST",
            "http://example.test/api/v1/repositories",
            json={
                "name": "ObjectRepo",
                "type": "s3",
                "path": None,
                "server": None,
                "share": None,
                "username": None,
                "password": None,
                "description": "MinIO lab repo",
                "s3": {
                    "bucket": "altastor-backups",
                    "prefix": "lab",
                    "region": "us-east-1",
                    "endpoint_url": "http://minio.local:9000",
                    "access_key": "MINIOACCESS",
                    "secret_key": "super-secret-key",
                    "description": "MinIO lab repo",
                    "path_style": True,
                    "verify_tls": False,
                },
            },
            params=None,
            timeout=30,
            headers={"X-Client-Type": "cli", "X-ABR-API-Version": "1.0"},
            verify=False,
        )

    def test_repo_add_command_rejects_invalid_s3_endpoint(self) -> None:
        args = SimpleNamespace(
            base_url="http://example.test",
            output="json",
            name="ObjectRepo",
            type="s3",
            path=None,
            address=None,
            server=None,
            share=None,
            username=None,
            password=None,
            description=None,
            bucket="altastor-backups",
            prefix="lab",
            region="us-east-1",
            endpoint_url="minio.local:9000",
            access_key=None,
            secret_key=None,
            profile=None,
            path_style=True,
            verify_tls=False,
        )

        with self.assertRaisesRegex(ValueError, "valid --endpoint-url"):
            RepositoryCommands().create(args)

    @patch("python.cli.api.client.requests.request")
    def test_repo_edit_command_updates_description_and_remote_address(self, request_mock: object) -> None:
        request_mock.side_effect = [
            _FakeCliResponse({"items": [{"id": "repo-1", "name": "Archive"}]}),
            _FakeCliResponse({"id": "repo-1", "name": "Archive", "type": "nfs", "path": "nas:/exports/archive"}),
            _FakeCliResponse(
                {
                    "id": "repo-1",
                    "name": "Archive",
                    "type": "nfs",
                    "path": "nas2:/exports/archive-2",
                    "description": "Updated archive tier",
                    "status": "available",
                }
            ),
        ]
        args = SimpleNamespace(
            base_url="http://example.test",
            output="json",
            name="Archive",
            id=None,
            new_name=None,
            path=None,
            address="nas2:/exports/archive-2",
            server=None,
            share=None,
            username=None,
            password=None,
            description="Updated archive tier",
        )

        with redirect_stdout(io.StringIO()):
            exit_code = RepositoryCommands().edit(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(request_mock.call_args_list[2].args, ("PATCH", "http://example.test/api/v1/repositories/repo-1"))
        self.assertEqual(
            request_mock.call_args_list[2].kwargs["json"],
            {
                "server": "nas2",
                "share": "/exports/archive-2",
                "description": "Updated archive tier",
            },
        )

    @patch("python.cli.api.client.requests.request")
    def test_repo_edit_command_renames_repository_selected_by_id(self, request_mock: object) -> None:
        request_mock.side_effect = [
            _FakeCliResponse({"id": "repo-1", "name": "Old", "type": "local", "path": "/mnt/backup"}),
            _FakeCliResponse({"id": "repo-1", "name": "New", "type": "local", "path": "/mnt/backup", "status": "available"}),
        ]
        args = SimpleNamespace(
            base_url="http://example.test",
            output="json",
            name=None,
            id="repo-1",
            new_name="New",
            path=None,
            address=None,
            server=None,
            share=None,
            username=None,
            password=None,
            description=None,
        )

        with redirect_stdout(io.StringIO()):
            exit_code = RepositoryCommands().edit(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(request_mock.call_args_list[1].args, ("PATCH", "http://example.test/api/v1/repositories/repo-1"))
        self.assertEqual(request_mock.call_args_list[1].kwargs["json"], {"name": "New"})

    @patch("python.cli.api.client.requests.request")
    def test_repo_verify_posts_and_prints_result(self, request_mock: object) -> None:
        request_mock.return_value = _FakeCliResponse(
            {"id": "repo-1", "name": "Archive", "status": "available", "message": "Verified"}
        )
        args = SimpleNamespace(base_url="http://example.test", output="json", name=None, id="repo-1")

        with redirect_stdout(io.StringIO()) as out:
            exit_code = RepositoryCommands().verify(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "available")
        request_mock.assert_called_once_with(
            "POST",
            "http://example.test/api/v1/repositories/repo-1/verify",
            json=None,
            params=None,
            timeout=21600,
            headers={"X-Client-Type": "cli", "X-ABR-API-Version": "1.0"},
            verify=False,
        )

    def test_repo_verify_shows_an_elapsed_time_indicator_when_run_from_a_tty(self) -> None:
        with patch("python.cli.commands.repository_commands.run_with_elapsed_indicator") as indicator_mock:
            indicator_mock.return_value = {"id": "repo-1", "status": "available"}
            args = SimpleNamespace(base_url="http://example.test", output="json", name=None, id="repo-1")
            with redirect_stdout(io.StringIO()):
                exit_code = RepositoryCommands().verify(args)

        self.assertEqual(exit_code, 0)
        indicator_mock.assert_called_once()
        self.assertEqual(indicator_mock.call_args.kwargs.get("label"), "Verifying repository")

    @patch("python.cli.api.client.requests.request")
    def test_repo_verify_reports_a_failed_check_as_a_non_zero_exit(self, request_mock: object) -> None:
        request_mock.return_value = _FakeCliResponse(
            {
                "id": "repo-1",
                "name": "Archive",
                "status": "error",
                "message": "Backup integrity check failed: 1 of 1 backup artifact(s) failed verification",
            }
        )
        args = SimpleNamespace(base_url="http://example.test", output="json", name=None, id="repo-1")

        with redirect_stdout(io.StringIO()) as out:
            exit_code = RepositoryCommands().verify(args)

        self.assertIn("Backup integrity check failed", out.getvalue())
        self.assertEqual(exit_code, INVALID_STATE)


class AbrV41EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        HYPERVISORS.clear()
        JOBS.clear()
        REPOSITORIES.clear()
        RESOURCES_BY_HV.clear()
        TASKS.clear()
        BACKUPS.clear()
        REPOSITORIES["repo-1"] = {"id": "repo-1", "name": "Repo", "type": "local", "path": "/tmp", "metadata": {"supports_synthetic_full": True}}
        REPOSITORIES["repo-2"] = {"id": "repo-2", "name": "NoSynth", "type": "smb", "path": "//host/share", "metadata": {"supports_synthetic_full": False}}

    def _job(self, **overrides):
        base = {
            "id": "job-1",
            "name": "Job",
            "job_type": "backup",
            "inventory_id": "inv-1",
            "vm_ids": ["vm-1"],
            "vm_names": ["vm-1"],
            "repo_id": "repo-1",
            "repo_name": "Repo",
            "schedule": {"kind": "daily", "time": "22:00", "timezone": "UTC"},
            "retention_days": 30,
            "gfs": {"weekly": 0, "monthly": 0, "yearly": 0},
            "active_full_days": [],
            "synthetic_full_days": [],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        base.update(overrides)
        return abr_v41.normalize_job_record(base)

    def test_first_run_and_force_full_resolution(self) -> None:
        self.assertEqual(abr_v41.resolve_job_execution(self._job(), vm_id="vm-1", force_full=False), ("ActiveFull", "FirstRun"))
        BACKUPS["p1"] = abr_v41.normalize_backup_point({"id": "p1", "job_id": "job-1", "vm_id": "vm-1", "repository_id": "repo-1", "execution_type": "ActiveFull"})
        self.assertEqual(abr_v41.resolve_job_execution(self._job(), vm_id="vm-1", force_full=False), ("Incremental", "StandardIncremental"))
        self.assertEqual(abr_v41.resolve_job_execution(self._job(), vm_id="vm-1", force_full=True), ("ActiveFull", "ForceFull"))

    def test_active_synthetic_priority_and_escalation(self) -> None:
        BACKUPS["p1"] = abr_v41.normalize_backup_point({"id": "p1", "job_id": "job-1", "vm_id": "vm-1", "repository_id": "repo-1", "execution_type": "ActiveFull"})
        today = abr_v41.DAY_ORDER[datetime.now().astimezone().weekday()]
        self.assertEqual(
            abr_v41.resolve_job_execution(self._job(active_full_days=[today], synthetic_full_days=[today]), vm_id="vm-1", force_full=False),
            ("ActiveFull", "ActiveOverridesSynthetic"),
        )
        self.assertEqual(
            abr_v41.resolve_job_execution(self._job(synthetic_full_days=[today]), vm_id="vm-1", force_full=False),
            ("SyntheticFull", "SyntheticFullWindow"),
        )
        BACKUPS["p2"] = abr_v41.normalize_backup_point({"id": "p2", "job_id": "job-1", "vm_id": "vm-1", "repository_id": "repo-2", "execution_type": "ActiveFull"})
        self.assertEqual(
            abr_v41.resolve_job_execution(self._job(repo_id="repo-2", repo_name="NoSynth", synthetic_full_days=[today]), vm_id="vm-1", force_full=False),
            ("ActiveFull", "RepositorySyntheticUnsupportedEscalatedToActive"),
        )

    def test_restore_force_rejected_outside_in_place(self) -> None:
        BACKUPS["p1"] = abr_v41.normalize_backup_point({"id": "p1", "job_id": "job-1", "vm_id": "vm-1", "repository_id": "repo-1", "execution_type": "ActiveFull"})
        with self.assertRaises(ApiError):
            abr_v41.restore_start({"point_id": "p1", "name": "Restored", "target_inventory": "missing", "force": True})

    def test_restore_start_requires_manifest_path(self) -> None:
        BACKUPS["p1"] = abr_v41.normalize_backup_point(
            {
                "id": "p1",
                "vm_id": "vm-1",
                "vm_name": "tstvm",
                "inventory_id": "inv-1",
                "inventory_name": "Inv",
                "repository_id": "repo-1",
                "repository_name": "Repo",
                "execution_type": "ActiveFull",
            }
        )

        with self.assertRaises(ApiError) as ctx:
            abr_v41.restore_start({"point_id": "p1"})

        self.assertEqual(ctx.exception.code, "restore_point_manifest_missing")

    def test_restore_point_catalog_filters_by_vm_backup_and_inventory(self) -> None:
        BACKUPS["rp-older"] = abr_v41.normalize_backup_point(
            {
                "id": "rp-older",
                "chain_id": "backup-1",
                "job_id": "job-1",
                "job_name": "Nightly",
                "vm_id": "vm-1",
                "vm_name": "Ubuntu-VM",
                "inventory_id": "inv-1",
                "inventory_name": "blackdog",
                "repository_id": "repo-1",
                "repository_name": "Repo",
                "execution_type": "ActiveFull",
                "created_at": "2026-05-25T20:00:00+00:00",
                "status": "available",
            }
        )
        BACKUPS["rp-latest"] = abr_v41.normalize_backup_point(
            {
                "id": "rp-latest",
                "chain_id": "backup-1",
                "job_id": "job-1",
                "job_name": "Nightly",
                "vm_id": "vm-1",
                "vm_name": "Ubuntu-VM",
                "inventory_id": "inv-1",
                "inventory_name": "blackdog",
                "repository_id": "repo-1",
                "repository_name": "Repo",
                "execution_type": "Incremental",
                "created_at": "2026-05-26T20:00:00+00:00",
                "status": "available",
            }
        )
        BACKUPS["rp-other"] = abr_v41.normalize_backup_point(
            {
                "id": "rp-other",
                "chain_id": "backup-2",
                "job_id": "job-2",
                "job_name": "Other",
                "vm_id": "vm-2",
                "vm_name": "Other-VM",
                "inventory_id": "inv-2",
                "inventory_name": "other",
                "repository_id": "repo-2",
                "repository_name": "NoSynth",
                "execution_type": "ActiveFull",
                "created_at": "2026-05-26T18:00:00+00:00",
                "status": "available",
            }
        )

        items = abr_v41.list_restore_points(vm="Ubuntu-VM", backup_id="backup-1", inventory="blackdog")

        self.assertEqual([item["id"] for item in items], ["rp-latest", "rp-older"])
        self.assertEqual(items[0]["backup_id"], "backup-1")
        self.assertEqual(items[0]["inventory_name"], "blackdog")
        self.assertEqual(items[0]["point_type"], "incremental")

    def test_backup_tree_groups_hierarchy_and_uses_latest_valid_restore_point(self) -> None:
        BACKUPS["rp-full"] = abr_v41.normalize_backup_point(
            {
                "id": "rp-full",
                "chain_id": "backup-1",
                "job_id": "job-1",
                "job_name": "Nightly",
                "vm_id": "vm-1",
                "vm_name": "Ubuntu-VM",
                "inventory_id": "inv-1",
                "inventory_name": "blackdog",
                "repository_id": "repo-1",
                "repository_name": "Repo",
                "execution_type": "ActiveFull",
                "created_at": "2026-05-25T20:00:00+00:00",
                "status": "available",
            }
        )
        BACKUPS["rp-inc"] = abr_v41.normalize_backup_point(
            {
                "id": "rp-inc",
                "chain_id": "backup-1",
                "job_id": "job-1",
                "job_name": "Nightly",
                "vm_id": "vm-1",
                "vm_name": "Ubuntu-VM",
                "inventory_id": "inv-1",
                "inventory_name": "blackdog",
                "repository_id": "repo-1",
                "repository_name": "Repo",
                "execution_type": "Incremental",
                "created_at": "2026-05-26T20:00:00+00:00",
                "status": "available",
            }
        )
        BACKUPS["rp-invalid"] = abr_v41.normalize_backup_point(
            {
                "id": "rp-invalid",
                "chain_id": "backup-1",
                "job_id": "job-1",
                "job_name": "Nightly",
                "vm_id": "vm-1",
                "vm_name": "Ubuntu-VM",
                "inventory_id": "inv-1",
                "inventory_name": "blackdog",
                "repository_id": "repo-1",
                "repository_name": "Repo",
                "execution_type": "Incremental",
                "created_at": "2026-05-27T20:00:00+00:00",
                "status": "missing",
            }
        )

        nodes = abr_v41.list_backup_tree()

        self.assertEqual(len(nodes), 1)
        repository_node = nodes[0]
        inventory_node = repository_node["children"][0]
        job_node = inventory_node["children"][0]
        protected_object_node = job_node["children"][0]
        chain_node = protected_object_node["children"][0]
        restore_point_nodes = chain_node["children"]

        self.assertEqual(repository_node["kind"], "repository")
        self.assertEqual(inventory_node["kind"], "inventory")
        self.assertEqual(job_node["kind"], "job")
        self.assertEqual(protected_object_node["kind"], "protected_object")
        self.assertEqual(chain_node["kind"], "backup_chain")
        self.assertEqual(chain_node["backup_type"], "incremental")
        self.assertTrue(protected_object_node["can_restore"])
        self.assertTrue(chain_node["can_restore"])
        self.assertEqual(protected_object_node["restore_payload"]["point_id"], "rp-inc")
        self.assertEqual(chain_node["restore_payload"]["point_id"], "rp-inc")
        self.assertEqual(
            [node["restore_point_id"] for node in restore_point_nodes],
            ["rp-invalid", "rp-inc", "rp-full"],
        )
        self.assertFalse(restore_point_nodes[0]["can_restore"])
        self.assertEqual(restore_point_nodes[0]["health"], "invalid")
        self.assertIn("status is missing", restore_point_nodes[0]["disabled_reason"])
        self.assertTrue(restore_point_nodes[1]["can_restore"])
        self.assertEqual(restore_point_nodes[1]["restore_payload"]["point_id"], "rp-inc")

    def test_restore_start_resolves_latest_point_from_backup_chain(self) -> None:
        inventory = abr_v41.create_inventory_record(
            {
                "name": "esxi",
                "type": "vsphere",
                "address": "192.168.12.37",
                "username": "root",
                "password": "secret",
            }
        )
        RESOURCES_BY_HV[inventory["id"]][("host", "esx-01.local")] = {
            "resource_type": "host",
            "external_id": "esx-01.local",
            "name": "esx-01.local",
            "payload": {"datastores": ["tstds"]},
            "discovered_at": datetime.now(timezone.utc).isoformat(),
        }
        BACKUPS["rp-older"] = abr_v41.normalize_backup_point(
            {
                "id": "rp-older",
                "chain_id": "backup-1",
                "vm_id": "vm-1",
                "vm_name": "tstvm",
                "inventory_id": "src-inv",
                "inventory_name": "src",
                "repository_id": "repo-1",
                "repository_name": "Repo",
                "execution_type": "ActiveFull",
                "created_at": "2026-05-25T20:00:00+00:00",
                "artifact": {"manifest_path": "/tmp/backups/tstvm/older.json", "backup_root": "/tmp/backups/tstvm"},
                "placements": [{"repository_id": "repo-1", "repository_name": "Repo", "manifest_path": "/tmp/backups/tstvm/older.json", "backup_root": "/tmp/backups/tstvm"}],
                "status": "available",
            }
        )
        BACKUPS["rp-latest"] = abr_v41.normalize_backup_point(
            {
                "id": "rp-latest",
                "chain_id": "backup-1",
                "vm_id": "vm-1",
                "vm_name": "tstvm",
                "inventory_id": "src-inv",
                "inventory_name": "src",
                "repository_id": "repo-1",
                "repository_name": "Repo",
                "execution_type": "Incremental",
                "created_at": "2026-05-26T20:00:00+00:00",
                "artifact": {"manifest_path": "/tmp/backups/tstvm/latest.json", "backup_root": "/tmp/backups/tstvm"},
                "placements": [{"repository_id": "repo-1", "repository_name": "Repo", "manifest_path": "/tmp/backups/tstvm/latest.json", "backup_root": "/tmp/backups/tstvm"}],
                "status": "available",
            }
        )

        session = abr_v41.restore_start(
            {
                "backup": "backup-1",
                "vm": "tstvm",
                "name": "restoredtstvm",
                "target_inventory_id": inventory["id"],
                "target_host": "192.168.12.37",
                "target_datastore": "tstds",
            }
        )

        self.assertEqual(session["restore_point_id"], "rp-latest")
        self.assertEqual(session["source_vm_name"], "tstvm")
        self.assertEqual(TASKS[session["id"]]["parameters"]["point_id"], "rp-latest")

    def test_restore_start_resolves_encryption_key_from_source_job(self) -> None:
        inventory = abr_v41.create_inventory_record(
            {
                "name": "esxi",
                "type": "vsphere",
                "address": "192.168.12.37",
                "username": "root",
                "password": "secret",
            }
        )
        RESOURCES_BY_HV[inventory["id"]][("host", "esx-01.local")] = {
            "resource_type": "host",
            "external_id": "esx-01.local",
            "name": "esx-01.local",
            "payload": {"datastores": ["tstds"]},
            "discovered_at": datetime.now(timezone.utc).isoformat(),
        }
        RESOURCES_BY_HV[inventory["id"]]["vm-1"] = {
            "resource_type": "virtual_machine",
            "external_id": "vm-1",
            "name": "tstvm",
            "payload": {},
            "discovered_at": datetime.now(timezone.utc).isoformat(),
        }

        job = abr_v41.create_backup_job(
            {
                "name": "Encrypted Nightly",
                "vms": "vm-1",
                "inventory_id": inventory["id"],
                "repo_id": "repo-1",
                "retention_days": 14,
                "task_parameters": {"encryption_enabled": True, "encryption_key": "hunter2"},
            }
        )

        BACKUPS["rp-encrypted"] = abr_v41.normalize_backup_point(
            {
                "id": "rp-encrypted",
                "job_id": job["id"],
                "chain_id": "backup-encrypted",
                "vm_id": "vm-1",
                "vm_name": "tstvm",
                "inventory_id": inventory["id"],
                "inventory_name": "esxi",
                "repository_id": "repo-1",
                "repository_name": "Repo",
                "execution_type": "ActiveFull",
                "created_at": "2026-05-25T20:00:00+00:00",
                "artifact": {"manifest_path": "/tmp/backups/tstvm/full.json", "backup_root": "/tmp/backups/tstvm"},
                "placements": [{"repository_id": "repo-1", "repository_name": "Repo", "manifest_path": "/tmp/backups/tstvm/full.json", "backup_root": "/tmp/backups/tstvm"}],
                "status": "available",
            }
        )

        session = abr_v41.restore_start(
            {
                "point_id": "rp-encrypted",
                "name": "restoredtstvm",
                "target_inventory_id": inventory["id"],
                "target_host": "192.168.12.37",
                "target_datastore": "tstds",
            }
        )

        self.assertEqual(TASKS[session["id"]]["parameters"]["encryption_key"], "hunter2")

    def test_restore_start_rejects_duplicate_target_vm_name(self) -> None:
        inventory = abr_v41.create_inventory_record(
            {
                "name": "esxi",
                "type": "vsphere",
                "address": "192.168.12.37",
                "username": "root",
                "password": "secret",
            }
        )
        RESOURCES_BY_HV[inventory["id"]][("host", "esx-01.local")] = {
            "resource_type": "host",
            "external_id": "esx-01.local",
            "name": "esx-01.local",
            "payload": {"datastores": ["tstds"]},
            "discovered_at": datetime.now(timezone.utc).isoformat(),
        }
        RESOURCES_BY_HV[inventory["id"]][("virtual_machine", "vm-existing")] = {
            "resource_type": "virtual_machine",
            "external_id": "vm-existing",
            "name": "restoredtstvm",
            "payload": {},
            "discovered_at": datetime.now(timezone.utc).isoformat(),
        }
        BACKUPS["p1"] = abr_v41.normalize_backup_point(
            {
                "id": "p1",
                "vm_id": "vm-1",
                "vm_name": "tstvm",
                "inventory_id": "src-inv",
                "inventory_name": "src",
                "repository_id": "repo-1",
                "repository_name": "Repo",
                "execution_type": "ActiveFull",
                "artifact": {"manifest_path": "/tmp/backups/tstvm/manifest.json", "backup_root": "/tmp/backups/tstvm"},
                "placements": [{"repository_id": "repo-1", "repository_name": "Repo", "manifest_path": "/tmp/backups/tstvm/manifest.json", "backup_root": "/tmp/backups/tstvm"}],
                "status": "available",
            }
        )

        with self.assertRaises(ApiError) as ctx:
            abr_v41.restore_start(
                {
                    "point_id": "p1",
                    "name": "restoredtstvm",
                    "target_inventory_id": inventory["id"],
                    "target_host": "192.168.12.37",
                    "target_datastore": "tstds",
                }
            )

        self.assertEqual(ctx.exception.code, "target_vm_exists")

    def _register_in_place_fixture(self, *, register_source_vm: bool = True, free_bytes: int | None = None) -> tuple[dict, str]:
        """A vCenter inventory with a host/datastore matching a backup's
        manifest (source_host=esx-01.local, disk on [tstds]) — the shared
        fixture shape in-place restore tests need so the source VM's
        original_host/original_datastore actually resolve, giving
        supports_in_place_restore a real True instead of falling back to
        restore_mode_unsupported before the behavior under test is reached."""
        inventory = abr_v41.create_inventory_record(
            {
                "name": f"vc-inplace-{len(HYPERVISORS)}",
                "type": "vsphere",
                "address": "192.168.12.37",
                "username": "root",
                "password": "secret",
            }
        )
        host_payload: dict[str, object] = {"datastores": ["tstds"]}
        if free_bytes is not None:
            host_payload["datastore_capacity"] = {"tstds": {"capacity_bytes": free_bytes * 4, "free_bytes": free_bytes}}
        RESOURCES_BY_HV[inventory["id"]][("host", "esx-01.local")] = {
            "resource_type": "host",
            "external_id": "esx-01.local",
            "name": "esx-01.local",
            "payload": host_payload,
            "discovered_at": datetime.now(timezone.utc).isoformat(),
        }
        if register_source_vm:
            RESOURCES_BY_HV[inventory["id"]][("virtual_machine", "vm-1")] = {
                "resource_type": "virtual_machine",
                "external_id": "vm-1",
                "name": "tstvm",
                "payload": {},
                "discovered_at": datetime.now(timezone.utc).isoformat(),
            }

        tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        manifest_path = f"{tmpdir}/manifest.json"
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "vm_name": "tstvm",
                    "vm_external_id": "vm-1",
                    "vm_config": {"name": "tstvm", "source_host": "esx-01.local"},
                    "disks": [{"disk_label": "Hard disk 1", "capacity_gb": 40, "datastore_path": "[tstds] tstvm/tstvm.vmdk"}],
                },
                handle,
            )
        point_id = f"p-ip-{len(BACKUPS)}"
        BACKUPS[point_id] = abr_v41.normalize_backup_point(
            {
                "id": point_id,
                "chain_id": f"backup-{point_id}",
                "vm_id": "vm-1",
                "vm_name": "tstvm",
                "inventory_id": inventory["id"],
                "inventory_name": inventory["name"],
                "repository_id": "repo-1",
                "repository_name": "Repo",
                "execution_type": "ActiveFull",
                "status": "available",
                "artifact": {"manifest_path": manifest_path, "backup_root": tmpdir},
                "placements": [{"repository_id": "repo-1", "repository_name": "Repo", "manifest_path": manifest_path, "backup_root": tmpdir}],
            }
        )
        return inventory, point_id

    def test_restore_start_in_place_requires_force(self) -> None:
        _inventory, point_id = self._register_in_place_fixture()

        with self.assertRaises(ApiError) as ctx:
            abr_v41.restore_start({"restore_point_id": point_id, "restore_mode": "in_place"})

        self.assertEqual(ctx.exception.code, "force_required")

    def test_restore_start_in_place_requires_source_vm_present(self) -> None:
        _inventory, point_id = self._register_in_place_fixture(register_source_vm=False)

        with self.assertRaises(ApiError) as ctx:
            abr_v41.restore_start({"restore_point_id": point_id, "restore_mode": "in_place", "force": True})

        self.assertEqual(ctx.exception.code, "in_place_source_vm_not_found")

    def test_restore_start_in_place_ignores_target_overrides(self) -> None:
        inventory, point_id = self._register_in_place_fixture()

        session = abr_v41.restore_start(
            {
                "restore_point_id": point_id,
                "restore_mode": "in_place",
                "force": True,
                # An attempted override — in-place restore must ignore all of
                # this and use the source VM's own original placement/name.
                "target": {
                    "inventory_id": inventory["id"],
                    "host_id": "some-other-host",
                    "datastore_id": "some-other-datastore",
                    "vm_name": "hijacked-name",
                },
            }
        )

        task = TASKS[session["id"]]
        self.assertEqual(task["parameters"]["target_host_name"], "esx-01.local")
        self.assertEqual(task["parameters"]["target_datastore_name"], "tstds")
        self.assertEqual(task["parameters"]["target_vm_name"], "tstvm")
        self.assertEqual(task["parameters"]["restore_mode"], "in_place")
        self.assertTrue(task["parameters"]["force"])

    def test_restore_session_dto_names_the_restored_vm(self) -> None:
        _inventory, point_id = self._register_in_place_fixture()

        session = abr_v41.restore_start(
            {
                "restore_point_id": point_id,
                "restore_mode": "new_vm",
                "target_vm_name": "restoredtstvm",
            }
        )

        # A restore has no job, so the console has nothing but these to identify
        # it: without them the Activity feed showed "—" for both the run name
        # and the virtual machine.
        self.assertEqual(session["type"], "restore")
        self.assertIsNone(session["job"])
        self.assertEqual(session["vm_name"], "restoredtstvm")
        self.assertEqual(session["source_vm_name"], "tstvm")

    def test_restore_start_capacity_check_scales_with_restore_mode(self) -> None:
        # The 40GB manifest disk needs ~43GB free for a 1x (new_vm) restore
        # and ~86GB for a 2x (in_place) restore — 50GB clears the first but
        # not the second.
        _inventory, point_id = self._register_in_place_fixture(free_bytes=50_000_000_000)

        session = abr_v41.restore_start(
            {
                "restore_point_id": point_id,
                "restore_mode": "new_vm",
                "target_vm_name": "restoredtstvm",
            }
        )
        self.assertEqual(session["restore_mode"], "new_vm")

        with self.assertRaises(ApiError) as ctx:
            abr_v41.restore_start({"restore_point_id": point_id, "restore_mode": "in_place", "force": True})

        self.assertEqual(ctx.exception.code, "insufficient_datastore_space")

    def test_inventory_test_success_persists_available_status(self) -> None:
        inventory = abr_v41.create_inventory_record(
            {
                "name": "Lab Hyper-V",
                "type": "hyper-v",
                "address": "192.168.12.50",
                "username": "administrator",
                "password": "secret",
            }
        )

        self.assertEqual(HYPERVISORS[inventory["id"]]["status"], "unknown")

        result = abr_v41.test_inventory(inventory["id"])

        self.assertEqual(result["status"], "success")
        self.assertEqual(HYPERVISORS[inventory["id"]]["status"], "available")

    def test_inventory_test_failure_persists_unavailable_status(self) -> None:
        inventory = abr_v41.create_inventory_record(
            {
                "name": "Broken Hyper-V",
                "type": "hyper-v",
                "address": "192.168.12.51",
                "username": "administrator",
                "password": "secret",
            }
        )
        HYPERVISORS[inventory["id"]]["metadata"]["credentials"]["password"] = None

        result = abr_v41.test_inventory(inventory["id"])

        self.assertEqual(result["status"], "error")
        self.assertEqual(HYPERVISORS[inventory["id"]]["status"], "unavailable")

    def test_create_inventory_rejects_duplicate_name(self) -> None:
        abr_v41.create_inventory_record(
            {
                "name": "Lab vCenter",
                "type": "vsphere",
                "address": "192.168.12.60",
                "username": "root",
                "password": "secret",
            }
        )

        with self.assertRaises(ApiError) as ctx:
            abr_v41.create_inventory_record(
                {
                    "name": "lab vcenter",  # same name, different case
                    "type": "vsphere",
                    "address": "192.168.12.61",
                    "username": "root",
                    "password": "secret",
                }
            )

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.code, "inventory_name_exists")
        self.assertEqual(len(HYPERVISORS), 1)

    def test_create_inventory_rejects_duplicate_address(self) -> None:
        first = abr_v41.create_inventory_record(
            {
                "name": "Lab vCenter",
                "type": "vsphere",
                "address": "192.168.12.60",
                "username": "root",
                "password": "secret",
            }
        )

        with self.assertRaises(ApiError) as ctx:
            abr_v41.create_inventory_record(
                {
                    "name": "Lab vCenter Copy",  # distinct name, same box
                    "type": "vsphere",
                    "address": "192.168.12.60",
                    "username": "root",
                    "password": "secret",
                }
            )

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.code, "inventory_address_exists")
        self.assertEqual(ctx.exception.details["existing_id"], first["id"])
        self.assertEqual(ctx.exception.details["existing_name"], "Lab vCenter")
        self.assertEqual(ctx.exception.details["port"], 443)
        # A rejected duplicate must not be persisted.
        self.assertEqual(len(HYPERVISORS), 1)

    def test_create_inventory_duplicate_address_matches_across_url_forms(self) -> None:
        abr_v41.create_inventory_record(
            {
                "name": "Lab vCenter",
                "type": "vsphere",
                "address": "https://vc.lab.local/sdk",
                "username": "root",
                "password": "secret",
            }
        )

        with self.assertRaises(ApiError) as ctx:
            abr_v41.create_inventory_record(
                {
                    "name": "Lab vCenter 2",
                    "type": "vsphere",
                    "address": "VC.LAB.LOCAL",  # same host, bare + different case
                    "username": "root",
                    "password": "secret",
                }
            )

        self.assertEqual(ctx.exception.code, "inventory_address_exists")

    def test_create_inventory_allows_same_host_on_a_different_port(self) -> None:
        abr_v41.create_inventory_record(
            {
                "name": "Lab vCenter",
                "type": "vsphere",
                "address": "192.168.12.60",
                "username": "root",
                "password": "secret",
            }
        )
        abr_v41.create_inventory_record(
            {
                "name": "Lab vCenter alt port",
                "type": "vsphere",
                "address": "192.168.12.60",
                "port": 8443,
                "username": "root",
                "password": "secret",
            }
        )

        self.assertEqual(len(HYPERVISORS), 2)

    def test_create_inventory_masks_the_token_in_the_response_like_the_password(self) -> None:
        inventory = abr_v41.create_inventory_record(
            {
                "name": "probe-token",
                "type": "vsphere",
                "address": "10.255.255.9",
                "username": "probe-user",
                "password": "Canary-Password-8c07",
                "token": "Canary-Token-4d91ac",
                "insecure": True,
            }
        )

        self.assertEqual(inventory["metadata"]["token"], "********")
        self.assertEqual(inventory["metadata"]["credentials"]["password"], "********")
        self.assertTrue(inventory["connection"]["token_present"])
        serialized = json.dumps(inventory)
        self.assertNotIn("Canary-Token-4d91ac", serialized)
        self.assertNotIn("Canary-Password-8c07", serialized)

    def test_list_and_lookup_also_mask_the_token_not_just_create(self) -> None:
        created = abr_v41.create_inventory_record(
            {
                "name": "probe-token-2",
                "type": "vsphere",
                "address": "10.255.255.10",
                "username": "probe-user",
                "password": "Canary-Password-2",
                "token": "Canary-Token-2",
                "insecure": True,
            }
        )

        listed = abr_v41.list_inventory_records()
        looked_up = abr_v41.resolve_inventory(record_id=created["id"])

        for record in (listed[0], looked_up):
            self.assertEqual(record["metadata"]["token"], "********")
            self.assertNotIn("Canary-Token-2", json.dumps(record))

    def test_inventory_without_a_token_reports_it_absent_not_masked(self) -> None:
        inventory = abr_v41.create_inventory_record(
            {
                "name": "no-token",
                "type": "vsphere",
                "address": "10.255.255.11",
                "username": "probe-user",
                "password": "secret",
            }
        )

        self.assertIsNone(inventory["metadata"]["token"])
        self.assertFalse(inventory["connection"]["token_present"])

    def test_create_inventory_rejects_clustered_esx_host(self) -> None:
        with patch(
            "python.srv.services.abr_v41.assert_inventory_registrable",
            side_effect=EsxHostInClusterError("prod-cluster"),
        ):
            with self.assertRaises(ApiError) as ctx:
                abr_v41.create_inventory_record(
                    {
                        "name": "Clustered ESX",
                        "type": "vsphere",
                        "address": "192.168.12.40",
                        "username": "root",
                        "password": "secret",
                    }
                )

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.code, "esx_host_in_cluster")
        self.assertEqual(ctx.exception.details["cluster"], "prod-cluster")
        # A rejected host must not be persisted.
        self.assertFalse(HYPERVISORS)

    def test_inventory_test_reports_clustered_esx_host(self) -> None:
        inventory = abr_v41.create_inventory_record(
            {
                "name": "Lab ESX",
                "type": "vsphere",
                "address": "192.168.12.41",
                "username": "root",
                "password": "secret",
            }
        )

        class _ClusteredProvider:
            def test_connection(self, record: dict[str, object]) -> None:
                raise EsxHostInClusterError("prod-cluster")

        with patch(
            "python.srv.services.abr_v41.InventoryProviderRegistry.resolve",
            return_value=_ClusteredProvider(),
        ):
            result = abr_v41.test_inventory(inventory["id"])

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["code"], "esx_host_in_cluster")
        self.assertEqual(result["cluster"], "prod-cluster")
        self.assertEqual(HYPERVISORS[inventory["id"]]["status"], "unavailable")

    def test_inventory_rescan_failure_persists_unavailable_status_and_returns_api_error(self) -> None:
        inventory = abr_v41.create_inventory_record(
            {
                "name": "Broken vCenter",
                "type": "vsphere",
                "address": "192.168.12.17",
                "username": "administrator@local.vsphere",
                "password": "secret",
            }
        )

        class _FailingProvider:
            def rescan(self, record: dict[str, object]) -> list[dict[str, object]]:
                raise RuntimeError("invalid credentials")

        with patch("python.srv.services.abr_v41.InventoryProviderRegistry.resolve", return_value=_FailingProvider()):
            with self.assertRaises(ApiError) as ctx:
                abr_v41.rescan_inventory(inventory["id"])

        self.assertEqual(ctx.exception.status_code, 502)
        self.assertEqual(ctx.exception.code, "inventory_rescan_failed")
        self.assertEqual(ctx.exception.details["inventory_id"], inventory["id"])
        self.assertEqual(HYPERVISORS[inventory["id"]]["status"], "unavailable")
        self.assertEqual(HYPERVISORS[inventory["id"]]["last_scan_error"], "invalid credentials")
        normalized = abr_v41.normalize_inventory_record(HYPERVISORS[inventory["id"]])
        self.assertEqual(normalized["discovery"]["last_scan_error"], "invalid credentials")

    def test_inventory_rescan_success_clears_a_previous_failure_reason(self) -> None:
        inventory = abr_v41.create_inventory_record(
            {
                "name": "Flaky vCenter",
                "type": "vsphere",
                "address": "192.168.12.18",
                "username": "administrator@local.vsphere",
                "password": "secret",
            }
        )

        class _FailingProvider:
            def rescan(self, record: dict[str, object]) -> list[dict[str, object]]:
                raise RuntimeError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")

        class _WorkingProvider:
            def rescan(self, record: dict[str, object]) -> list[dict[str, object]]:
                return []

        with patch("python.srv.services.abr_v41.InventoryProviderRegistry.resolve", return_value=_FailingProvider()):
            with self.assertRaises(ApiError):
                abr_v41.rescan_inventory(inventory["id"])
        self.assertTrue(HYPERVISORS[inventory["id"]]["last_scan_error"])

        with patch("python.srv.services.abr_v41.InventoryProviderRegistry.resolve", return_value=_WorkingProvider()):
            abr_v41.rescan_inventory(inventory["id"])

        self.assertEqual(HYPERVISORS[inventory["id"]]["status"], "available")
        self.assertIsNone(HYPERVISORS[inventory["id"]]["last_scan_error"])
        normalized = abr_v41.normalize_inventory_record(HYPERVISORS[inventory["id"]])
        self.assertIsNone(normalized["discovery"]["last_scan_error"])

    def test_inventory_rescan_reconciles_job_after_vm_reappears_with_a_new_id(self) -> None:
        inventory = abr_v41.create_inventory_record(
            {
                "name": "Restore-prone vCenter",
                "type": "vsphere",
                "address": "192.168.12.19",
                "username": "administrator@local.vsphere",
                "password": "secret",
            }
        )
        inventory_id = inventory["id"]
        RESOURCES_BY_HV[inventory_id] = {
            ("virtual_machine", "vm-uuid-old"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-old",
                "name": "demo-vm",
                "payload": {},
            }
        }
        JOBS["job-inplace"] = self._job(id="job-inplace", inventory_id=inventory_id, vm_ids=["vm-uuid-old"], vm_names=["demo-vm"])
        JOBS["job-untouched"] = self._job(
            id="job-untouched", inventory_id=inventory_id, vm_ids=["vm-uuid-other"], vm_names=["other-vm"]
        )

        class _RenamedVmProvider:
            def rescan(self, record: dict[str, object]) -> list[dict[str, object]]:
                return [
                    {
                        "resource_type": "virtual_machine",
                        "external_id": "vm-uuid-new",
                        "name": "demo-vm",
                        "payload": {},
                    },
                    {
                        "resource_type": "virtual_machine",
                        "external_id": "vm-uuid-other",
                        "name": "other-vm",
                        "payload": {},
                    },
                ]

        with patch("python.srv.services.abr_v41.InventoryProviderRegistry.resolve", return_value=_RenamedVmProvider()):
            abr_v41.rescan_inventory(inventory_id)

        self.assertEqual(JOBS["job-inplace"]["vm_ids"], ["vm-uuid-new"])
        self.assertEqual(JOBS["job-inplace"]["vm_names"], ["demo-vm"])
        self.assertEqual(JOBS["job-untouched"]["vm_ids"], ["vm-uuid-other"])

    def test_inventory_rescan_reconciles_only_the_renamed_vm_in_a_multi_vm_job(self) -> None:
        inventory = abr_v41.create_inventory_record(
            {
                "name": "Restore-prone vCenter",
                "type": "vsphere",
                "address": "192.168.12.19",
                "username": "administrator@local.vsphere",
                "password": "secret",
            }
        )
        inventory_id = inventory["id"]
        RESOURCES_BY_HV[inventory_id] = {
            ("virtual_machine", "vm-uuid-old"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-old",
                "name": "demo-vm",
                "payload": {},
            },
            ("virtual_machine", "vm-uuid-stable"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-stable",
                "name": "other-vm",
                "payload": {},
            },
        }
        JOBS["job-multi"] = self._job(
            id="job-multi",
            inventory_id=inventory_id,
            vm_ids=["vm-uuid-old", "vm-uuid-stable"],
            vm_names=["demo-vm", "other-vm"],
        )

        class _OneVmRenamedProvider:
            def rescan(self, record: dict[str, object]) -> list[dict[str, object]]:
                return [
                    {
                        "resource_type": "virtual_machine",
                        "external_id": "vm-uuid-new",
                        "name": "demo-vm",
                        "payload": {},
                    },
                    {
                        "resource_type": "virtual_machine",
                        "external_id": "vm-uuid-stable",
                        "name": "other-vm",
                        "payload": {},
                    },
                ]

        with patch("python.srv.services.abr_v41.InventoryProviderRegistry.resolve", return_value=_OneVmRenamedProvider()):
            abr_v41.rescan_inventory(inventory_id)

        self.assertEqual(JOBS["job-multi"]["vm_ids"], ["vm-uuid-new", "vm-uuid-stable"])
        self.assertEqual(JOBS["job-multi"]["vm_names"], ["demo-vm", "other-vm"])

    def test_inventory_rescan_reconciles_a_backup_copy_job_the_same_as_a_backup_job(self) -> None:
        inventory = abr_v41.create_inventory_record(
            {
                "name": "Restore-prone vCenter",
                "type": "vsphere",
                "address": "192.168.12.19",
                "username": "administrator@local.vsphere",
                "password": "secret",
            }
        )
        inventory_id = inventory["id"]
        RESOURCES_BY_HV[inventory_id] = {
            ("virtual_machine", "vm-uuid-old"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-uuid-old",
                "name": "demo-vm",
                "payload": {},
            }
        }
        JOBS["job-copy"] = self._job(
            id="job-copy",
            job_type="backup-copy",
            inventory_id=inventory_id,
            vm_ids=["vm-uuid-old"],
            vm_names=["demo-vm"],
            source_job_id="job-source",
            source_job_name="Job source",
        )

        class _RenamedVmProvider:
            def rescan(self, record: dict[str, object]) -> list[dict[str, object]]:
                return [
                    {
                        "resource_type": "virtual_machine",
                        "external_id": "vm-uuid-new",
                        "name": "demo-vm",
                        "payload": {},
                    }
                ]

        with patch("python.srv.services.abr_v41.InventoryProviderRegistry.resolve", return_value=_RenamedVmProvider()):
            abr_v41.rescan_inventory(inventory_id)

        self.assertEqual(JOBS["job-copy"]["job_type"], "backup-copy")
        self.assertEqual(JOBS["job-copy"]["vm_ids"], ["vm-uuid-new"])

    def test_create_backup_job_without_schedule_is_planned(self) -> None:
        inventory = abr_v41.create_inventory_record(
            {
                "name": "Prod Hyper-V",
                "type": "hyper-v",
                "address": "192.168.12.40",
                "username": "administrator",
                "password": "secret",
            }
        )
        RESOURCES_BY_HV[inventory["id"]]["vm-1"] = {
            "resource_type": "virtual_machine",
            "external_id": "vm-1",
            "name": "vm-1",
            "payload": {},
            "discovered_at": datetime.now(timezone.utc).isoformat(),
        }

        job = abr_v41.create_backup_job(
            {
                "name": "Nightly",
                "vms": "vm-1",
                "inventory_id": inventory["id"],
                "repo_id": "repo-1",
                "retention_days": 14,
            }
        )

        self.assertIsNone(job["schedule"])
        self.assertIsNone(job["schedule_summary"])
        self.assertIsNone(job["next_run_at"])
        self.assertEqual(job["status"], "planned")

    def test_create_repository_record_accepts_remote_address_and_preserves_description(self) -> None:
        repository = abr_v41.create_repository_record(
            {
                "name": "Archive",
                "type": "smb",
                "address": "//nas/archive",
                "username": "backup",
                "password": "secret",
                "description": "Archive tier",
            }
        )

        self.assertEqual(repository["path"], "//nas/archive")
        self.assertEqual(repository["address"], "//nas/archive")
        self.assertEqual(repository["server"], "nas")
        self.assertEqual(repository["share"], "archive")
        self.assertEqual(repository["username"], "backup")
        self.assertEqual(repository["description"], "Archive tier")
        self.assertEqual(REPOSITORIES[repository["id"]]["metadata"]["description"], "Archive tier")

    def test_edit_repository_record_updates_remote_address_and_description(self) -> None:
        repository = abr_v41.create_repository_record(
            {
                "name": "Archive",
                "type": "nfs",
                "address": "nas:/exports/archive",
            }
        )

        updated = abr_v41.edit_repository_record(
            repository["id"],
            {
                "address": "nas2:/exports/archive-2",
                "description": "Updated archive tier",
                "username": "nfs-user",
            },
        )

        self.assertEqual(updated["path"], "nas2:/exports/archive-2")
        self.assertEqual(updated["address"], "nas2:/exports/archive-2")
        self.assertEqual(updated["server"], "nas2")
        self.assertEqual(updated["share"], "/exports/archive-2")
        self.assertEqual(updated["username"], "nfs-user")
        self.assertEqual(updated["description"], "Updated archive tier")

    def test_edit_repository_record_blocks_local_path_change_when_backups_exist(self) -> None:
        REPOSITORIES.clear()
        BACKUPS.clear()
        old_path = tempfile.mkdtemp(prefix="abr-repo-old-")
        new_path = tempfile.mkdtemp(prefix="abr-repo-new-")
        repository = abr_v41.create_repository_record({"name": "aqa-d34", "type": "local", "path": old_path})
        BACKUPS["p1"] = abr_v41.normalize_backup_point(
            {"id": "p1", "vm_id": "vm-1", "repository_id": repository["id"], "execution_type": "ActiveFull"}
        )

        with self.assertRaises(ApiError) as ctx:
            abr_v41.edit_repository_record(repository["id"], {"path": new_path})

        self.assertEqual(ctx.exception.code, "repository_has_backups")
        self.assertEqual(REPOSITORIES[repository["id"]]["path"], old_path)

    def test_edit_repository_record_allows_local_path_change_when_no_backups_exist(self) -> None:
        REPOSITORIES.clear()
        BACKUPS.clear()
        old_path = tempfile.mkdtemp(prefix="abr-repo-old-")
        new_path = tempfile.mkdtemp(prefix="abr-repo-new-")
        repository = abr_v41.create_repository_record({"name": "aqa-d34", "type": "local", "path": old_path})

        updated = abr_v41.edit_repository_record(repository["id"], {"path": new_path})

        self.assertEqual(updated["path"], new_path)

    def test_edit_repository_record_blocks_s3_bucket_change_when_backups_exist(self) -> None:
        REPOSITORIES.clear()
        BACKUPS.clear()
        repository = abr_v41.create_repository_record(
            {"name": "aqa-s3", "type": "s3", "bucket": "old-bucket", "region": "us-east-1"}
        )
        BACKUPS["p1"] = abr_v41.normalize_backup_point(
            {"id": "p1", "vm_id": "vm-1", "repository_id": repository["id"], "execution_type": "ActiveFull"}
        )

        with self.assertRaises(ApiError) as ctx:
            abr_v41.edit_repository_record(repository["id"], {"bucket": "new-bucket"})

        self.assertEqual(ctx.exception.code, "repository_has_backups")

    def test_verify_repository_fails_instead_of_reporting_success_when_repointed_without_moving_data(self) -> None:
        REPOSITORIES.clear()
        BACKUPS.clear()
        old_path = tempfile.mkdtemp(prefix="abr-repo-old-")
        new_path = tempfile.mkdtemp(prefix="abr-repo-new-")
        repository = abr_v41.create_repository_record({"name": "aqa-d34", "type": "local", "path": old_path})
        REPOSITORIES[repository["id"]]["path"] = new_path
        BACKUPS["p1"] = abr_v41.normalize_backup_point(
            {
                "id": "p1",
                "vm_id": "vm-1",
                "repository_id": repository["id"],
                "execution_type": "ActiveFull",
                "artifact": {"disks": [{"abr_path": old_path}]},
            }
        )

        result = abr_v41.verify_repository(repository["id"])

        self.assertEqual(result["status"], "error")
        self.assertIn(old_path, result["message"])

    def test_verify_repository_still_succeeds_when_the_path_matches_recorded_backups(self) -> None:
        REPOSITORIES.clear()
        BACKUPS.clear()
        path = tempfile.mkdtemp(prefix="abr-repo-")
        artifact_dir = Path(path) / "vm-1" / "task-1"
        artifact_dir.mkdir(parents=True)
        artifact_file = artifact_dir / "01-vm.vmdk"
        artifact_file.write_bytes(b"fake disk data")
        repository = abr_v41.create_repository_record({"name": "aqa-d34", "type": "local", "path": path})
        BACKUPS["p1"] = abr_v41.normalize_backup_point(
            {
                "id": "p1",
                "vm_id": "vm-1",
                "repository_id": repository["id"],
                "execution_type": "ActiveFull",
                "artifact": {"disks": [{"abr_path": str(artifact_dir), "destination_path": str(artifact_file)}]},
            }
        )

        result = abr_v41.verify_repository(repository["id"])

        self.assertEqual(result["status"], "success")

    @patch("python.srv.services.abr_v41.utc_now", return_value="2026-05-25T12:00:00+00:00")
    def test_rescan_repository_updates_last_scan_at(self, _utc_now: object) -> None:
        REPOSITORIES.clear()
        repository = abr_v41.create_repository_record(
            {
                "name": "Local",
                "type": "local",
                "path": tempfile.mkdtemp(prefix="abr-rescan-repo-"),
            }
        )

        payload = abr_v41.rescan_repository(repository["id"])

        self.assertEqual(payload["last_scan_at"], "2026-05-25T12:00:00+00:00")
        self.assertEqual(REPOSITORIES[repository["id"]]["metadata"]["last_scan_at"], "2026-05-25T12:00:00+00:00")

    def test_remove_repository_record_is_blocked_when_job_references_it(self) -> None:
        REPOSITORIES.clear()
        repository = abr_v41.create_repository_record(
            {
                "name": "Local",
                "type": "local",
                "path": tempfile.mkdtemp(prefix="abr-remove-repo-"),
            }
        )
        JOBS["job-1"] = {
            "id": "job-1",
            "name": "Nightly",
            "job_type": "backup",
            "repo_id": repository["id"],
            "repo_name": repository["name"],
            "retention_days": 14,
        }

        with self.assertRaises(ApiError) as ctx:
            abr_v41.remove_repository_record(repository["id"])

        self.assertEqual(ctx.exception.code, "repository_in_use")

    def test_remove_inventory_record_rejects_unknown_id(self) -> None:
        HYPERVISORS.clear()

        with self.assertRaises(ApiError) as ctx:
            abr_v41.remove_inventory_record("00000000-0000-0000-0000-000000000000")

        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.code, "inventory_not_found")

    def test_delete_job_record_rejects_unknown_id(self) -> None:
        JOBS.clear()

        with self.assertRaises(ApiError) as ctx:
            abr_v41.delete_job_record("00000000-0000-0000-0000-000000000000")

        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.code, "job_not_found")

    @patch("python.srv.services.abr_v41.utc_now", return_value="2026-05-24T23:30:00+00:00")
    @patch("python.srv.services.abr_v41._server_timezone_name", return_value="America/New_York")
    def test_create_backup_job_daily_schedule_uses_canonical_local_timezone(
        self,
        _server_timezone_name: object,
        _utc_now: object,
    ) -> None:
        inventory = abr_v41.create_inventory_record(
            {
                "name": "Prod Hyper-V",
                "type": "hyper-v",
                "address": "192.168.12.42",
                "username": "administrator",
                "password": "secret",
            }
        )
        RESOURCES_BY_HV[inventory["id"]]["vm-1"] = {
            "resource_type": "virtual_machine",
            "external_id": "vm-1",
            "name": "vm-1",
            "payload": {},
            "discovered_at": "2026-05-24T23:30:00+00:00",
        }

        job = abr_v41.create_backup_job(
            {
                "name": "Nightly",
                "vms": "vm-1",
                "inventory_id": inventory["id"],
                "repo_id": "repo-1",
                "retention_days": 14,
                "schedule": "daily 20:00",
            }
        )

        self.assertEqual(job["schedule"]["timezone"], "America/New_York")
        self.assertEqual(job["schedule_summary"], "Daily at 20:00 (America/New_York)")
        self.assertEqual(job["next_run_at"], "2026-05-25T00:00:00+00:00")

    def test_create_copy_job_without_schedule_is_planned(self) -> None:
        inventory = abr_v41.create_inventory_record(
            {
                "name": "Source Hyper-V",
                "type": "hyper-v",
                "address": "192.168.12.41",
                "username": "administrator",
                "password": "secret",
            }
        )
        RESOURCES_BY_HV[inventory["id"]]["vm-1"] = {
            "resource_type": "virtual_machine",
            "external_id": "vm-1",
            "name": "vm-1",
            "payload": {},
            "discovered_at": datetime.now(timezone.utc).isoformat(),
        }
        source_job = abr_v41.create_backup_job(
            {
                "name": "Source Job",
                "vms": "vm-1",
                "inventory_id": inventory["id"],
                "repo_id": "repo-1",
                "retention_days": 14,
            }
        )

        job = abr_v41.create_copy_job(
            {
                "name": "Archive Job",
                "source_job_id": source_job["id"],
                "repo_id": "repo-2",
                "retention_days": 21,
            }
        )

        self.assertIsNone(job["schedule"])
        self.assertIsNone(job["schedule_summary"])
        self.assertIsNone(job["next_run_at"])
        self.assertEqual(job["status"], "planned")

    @patch("python.srv.services.abr_v41._server_timezone_name", return_value="America/New_York")
    def test_legacy_schedule_timezone_abbreviation_uses_server_zone(self, _server_timezone_name: object) -> None:
        next_run_at = abr_v41.schedule_next_run(
            {"kind": "daily", "time": "20:00", "timezone": "EDT"},
            after=datetime(2026, 5, 24, 23, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(next_run_at.isoformat(), "2026-05-25T00:00:00+00:00")

    def test_restore_start_accepts_inventory_endpoint_for_single_host_inventory(self) -> None:
        inventory = abr_v41.create_inventory_record(
            {
                "name": "esxi",
                "type": "vsphere",
                "address": "192.168.12.37",
                "username": "root",
                "password": "secret",
            }
        )
        RESOURCES_BY_HV[inventory["id"]][("host", "esx-01.local")] = {
            "resource_type": "host",
            "external_id": "esx-01.local",
            "name": "esx-01.local",
            "payload": {"datastores": ["tstds"]},
            "discovered_at": datetime.now(timezone.utc).isoformat(),
        }
        BACKUPS["p1"] = abr_v41.normalize_backup_point(
            {
                "id": "p1",
                "vm_id": "vm-1",
                "vm_name": "tstvm",
                "inventory_id": "src-inv",
                "inventory_name": "src",
                "repository_id": "repo-1",
                "repository_name": "Repo",
                "execution_type": "ActiveFull",
                "artifact": {
                    "manifest_path": "/tmp/backups/tstvm/manifest.json",
                    "backup_root": "/tmp/backups/tstvm",
                },
                "placements": [
                    {
                        "repository_id": "repo-1",
                        "repository_name": "Repo",
                        "manifest_path": "/tmp/backups/tstvm/manifest.json",
                        "backup_root": "/tmp/backups/tstvm",
                    }
                ],
            }
        )

        session = abr_v41.restore_start(
            {
                "point_id": "p1",
                "name": "restoredtstvm",
                "target_inventory_id": inventory["id"],
                "target_host": "192.168.12.37",
                "target_datastore": "tstds",
                "network_map": ["VM Network=Recovery Network"],
            }
        )

        task = TASKS[session["id"]]
        self.assertEqual(task["parameters"]["target_host"], "192.168.12.37")
        self.assertEqual(task["parameters"]["target_host_name"], "esx-01.local")
        self.assertEqual(task["parameters"]["target_datastore_name"], "tstds")
        self.assertEqual(task["parameters"]["manifest_path"], "/tmp/backups/tstvm/manifest.json")
        self.assertEqual(task["parameters"]["backup_root"], "/tmp/backups/tstvm")
        self.assertEqual(task["parameters"]["network_map"], {"VM Network": "Recovery Network"})
        self.assertEqual(session["restore_session_id"], session["id"])
        self.assertEqual(session["status"], "started")
        self.assertEqual(session["restore_point_id"], "p1")
        self.assertEqual(session["source_vm_name"], "tstvm")
        self.assertEqual(session["target_inventory_id"], inventory["id"])
        self.assertEqual(session["target_host_name"], "esx-01.local")
        self.assertEqual(session["datastore_name"], "tstds")
        self.assertEqual(session["new_name"], "restoredtstvm")

    def test_restore_start_accepts_explicit_restore_request_and_preserves_efi_firmware(self) -> None:
        inventory = abr_v41.create_inventory_record(
            {
                "name": "vc-lab",
                "type": "vsphere",
                "address": "192.168.12.37",
                "username": "root",
                "password": "secret",
            }
        )
        RESOURCES_BY_HV[inventory["id"]][("host", "esx-01.local")] = {
            "resource_type": "host",
            "external_id": "esx-01.local",
            "name": "esx-01.local",
            "payload": {"datastores": ["tstds"]},
            "discovered_at": datetime.now(timezone.utc).isoformat(),
        }
        RESOURCES_BY_HV[inventory["id"]][("virtual_machine", "vm-1")] = {
            "resource_type": "virtual_machine",
            "external_id": "vm-1",
            "name": "tstvm",
            "payload": {
                "guest_os": "Ubuntu Linux (64-bit)",
                "num_cpu": 2,
                "memory_mb": 4096,
                "firmware": "efi",
                "host_external_id": "esx-01.local",
            },
            "discovered_at": datetime.now(timezone.utc).isoformat(),
        }
        RESOURCES_BY_HV[inventory["id"]][("network_adapter", "vm-1-nic-1")] = {
            "resource_type": "network_adapter",
            "external_id": "vm-1-nic-1",
            "name": "Network adapter 1",
            "payload": {
                "vm_id": "vm-1",
                "network": "VM Network",
                "mac": "00:50:56:aa:bb:cc",
                "connected": True,
            },
            "discovered_at": datetime.now(timezone.utc).isoformat(),
        }
        RESOURCES_BY_HV[inventory["id"]][("network_adapter", "vm-other-nic-1")] = {
            "resource_type": "network_adapter",
            "external_id": "vm-other-nic-1",
            "name": "Network adapter 1",
            "payload": {
                "vm_id": "vm-other",
                "network": "Recovery Network",
                "mac": "00:50:56:11:22:33",
                "connected": True,
            },
            "discovered_at": datetime.now(timezone.utc).isoformat(),
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = f"{tmpdir}/manifest.json"
            with open(manifest_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "vm_name": "tstvm",
                        "vm_external_id": "vm-1",
                        "vm_config": {
                            "name": "tstvm",
                            "instance_uuid": "instance-uuid-1",
                            "guest_os": "Ubuntu Linux (64-bit)",
                            "num_cpu": 2,
                            "memory_mb": 4096,
                            "firmware": "efi",
                            "source_host": "esx-01.local",
                            "network_adapters": [
                                {
                                    "label": "Network adapter 1",
                                    "network": "VM Network",
                                    "connected": True,
                                }
                            ],
                        },
                        "disks": [
                            {
                                "disk_label": "Hard disk 1",
                                "capacity_gb": 40,
                                "datastore_path": "[tstds] tstvm/tstvm.vmdk",
                                "source_disk_type_name": "vmfs_thin",
                            }
                        ],
                    },
                    handle,
                )
            BACKUPS["p-efi"] = abr_v41.normalize_backup_point(
                {
                    "id": "p-efi",
                    "chain_id": "backup-efi",
                    "vm_id": "vm-1",
                    "vm_name": "tstvm",
                    "inventory_id": inventory["id"],
                    "inventory_name": inventory["name"],
                    "repository_id": "repo-1",
                    "repository_name": "Repo",
                    "execution_type": "Incremental",
                    "status": "available",
                    "artifact": {"manifest_path": manifest_path, "backup_root": tmpdir},
                    "placements": [{"repository_id": "repo-1", "repository_name": "Repo", "manifest_path": manifest_path, "backup_root": tmpdir}],
                }
            )

            session = abr_v41.restore_start(
                {
                    "restore_point_id": "p-efi",
                    "restore_mode": "new_vm",
                    "target": {
                        "inventory_id": inventory["id"],
                        "host_id": "esx-01.local",
                        "datastore_id": f"{inventory['id']}::datastore::tstds",
                        "vm_name": "restoredtstvm",
                    },
                    "options": {
                        "power_on_after_restore": True,
                        "preserve_firmware": True,
                        "connect_nics": False,
                        "disk_provisioning": "thin",
                    },
                    "network_mappings": [
                        {
                            "source_nic_id": "vm-1-nic-1",
                            "target_network_id": f"{inventory['id']}::network::Recovery Network",
                        }
                    ],
                }
            )

        task = TASKS[session["id"]]
        self.assertEqual(task["parameters"]["point_id"], "p-efi")
        self.assertEqual(task["parameters"]["target_host_id"], "esx-01.local")
        self.assertEqual(task["parameters"]["target_datastore_id"], f"{inventory['id']}::datastore::tstds")
        self.assertEqual(task["parameters"]["target_vm_name"], "restoredtstvm")
        self.assertEqual(task["parameters"]["network_map"], {"VM Network": "Recovery Network"})
        self.assertEqual(task["parameters"]["firmware"], "efi")
        self.assertFalse(task["parameters"]["connect_nics"])
        self.assertEqual(task["parameters"]["disk_type"], "thin")
        self.assertEqual(session["restore_point_id"], "p-efi")
        self.assertEqual(session["restore_mode"], "new_vm")

    def test_restore_start_rejects_invalid_target_datastore_identifier(self) -> None:
        inventory = abr_v41.create_inventory_record(
            {
                "name": "vc-datastore",
                "type": "vsphere",
                "address": "192.168.12.37",
                "username": "root",
                "password": "secret",
            }
        )
        RESOURCES_BY_HV[inventory["id"]][("host", "esx-01.local")] = {
            "resource_type": "host",
            "external_id": "esx-01.local",
            "name": "esx-01.local",
            "payload": {"datastores": ["tstds"]},
            "discovered_at": datetime.now(timezone.utc).isoformat(),
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = f"{tmpdir}/manifest.json"
            with open(manifest_path, "w", encoding="utf-8") as handle:
                json.dump({"vm_name": "tstvm", "vm_external_id": "vm-1", "vm_config": {"name": "tstvm"}, "disks": [{"disk_label": "Hard disk 1", "capacity_gb": 40, "datastore_path": "[tstds] tstvm/tstvm.vmdk"}]}, handle)
            BACKUPS["p-ds"] = abr_v41.normalize_backup_point(
                {
                    "id": "p-ds",
                    "chain_id": "backup-ds",
                    "vm_id": "vm-1",
                    "vm_name": "tstvm",
                    "inventory_id": inventory["id"],
                    "inventory_name": inventory["name"],
                    "repository_id": "repo-1",
                    "repository_name": "Repo",
                    "execution_type": "ActiveFull",
                    "status": "available",
                    "artifact": {"manifest_path": manifest_path, "backup_root": tmpdir},
                    "placements": [{"repository_id": "repo-1", "repository_name": "Repo", "manifest_path": manifest_path, "backup_root": tmpdir}],
                }
            )

            with self.assertRaises(ApiError) as ctx:
                abr_v41.restore_start(
                    {
                        "restore_point_id": "p-ds",
                        "target": {
                            "inventory_id": inventory["id"],
                            "host_id": "esx-01.local",
                            "datastore_id": f"{inventory['id']}::datastore::missing",
                            "vm_name": "restoredtstvm",
                        },
                    }
                )

        self.assertEqual(ctx.exception.code, "datastore_not_found")

    def test_restore_start_rejects_unknown_target_network_id(self) -> None:
        inventory = abr_v41.create_inventory_record(
            {
                "name": "vc-network",
                "type": "vsphere",
                "address": "192.168.12.37",
                "username": "root",
                "password": "secret",
            }
        )
        RESOURCES_BY_HV[inventory["id"]][("host", "esx-01.local")] = {
            "resource_type": "host",
            "external_id": "esx-01.local",
            "name": "esx-01.local",
            "payload": {"datastores": ["tstds"]},
            "discovered_at": datetime.now(timezone.utc).isoformat(),
        }
        RESOURCES_BY_HV[inventory["id"]][("network_adapter", "vm-1-nic-1")] = {
            "resource_type": "network_adapter",
            "external_id": "vm-1-nic-1",
            "name": "Network adapter 1",
            "payload": {"vm_id": "vm-1", "network": "VM Network", "mac": "00:50:56:aa:bb:cc", "connected": True},
            "discovered_at": datetime.now(timezone.utc).isoformat(),
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_path = f"{tmpdir}/manifest.json"
            with open(manifest_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "vm_name": "tstvm",
                        "vm_external_id": "vm-1",
                        "vm_config": {
                            "name": "tstvm",
                            "firmware": "efi",
                            "network_adapters": [{"label": "Network adapter 1", "network": "VM Network", "connected": True}],
                        },
                        "disks": [{"disk_label": "Hard disk 1", "capacity_gb": 40, "datastore_path": "[tstds] tstvm/tstvm.vmdk"}],
                    },
                    handle,
                )
            BACKUPS["p-net"] = abr_v41.normalize_backup_point(
                {
                    "id": "p-net",
                    "chain_id": "backup-net",
                    "vm_id": "vm-1",
                    "vm_name": "tstvm",
                    "inventory_id": inventory["id"],
                    "inventory_name": inventory["name"],
                    "repository_id": "repo-1",
                    "repository_name": "Repo",
                    "execution_type": "Incremental",
                    "status": "available",
                    "artifact": {"manifest_path": manifest_path, "backup_root": tmpdir},
                    "placements": [{"repository_id": "repo-1", "repository_name": "Repo", "manifest_path": manifest_path, "backup_root": tmpdir}],
                }
            )

            with self.assertRaises(ApiError) as ctx:
                abr_v41.restore_start(
                    {
                        "restore_point_id": "p-net",
                        "target": {
                            "inventory_id": inventory["id"],
                            "host_id": "esx-01.local",
                            "datastore_id": f"{inventory['id']}::datastore::tstds",
                            "vm_name": "restoredtstvm",
                        },
                        "network_mappings": [
                            {
                                "source_nic_id": "vm-1-nic-1",
                                "target_network_id": f"{inventory['id']}::network::missing",
                            }
                        ],
                    }
                )

        self.assertEqual(ctx.exception.code, "network_not_found")

    def _single_nic_network_map_fixture(self, point_id: str, tmpdir: str) -> dict[str, object]:
        inventory = abr_v41.create_inventory_record(
            {
                "name": f"vc-legacy-netmap-{len(HYPERVISORS)}",
                "type": "vsphere",
                "address": "192.168.12.38",
                "username": "root",
                "password": "secret",
            }
        )
        RESOURCES_BY_HV[inventory["id"]][("host", "esx-01.local")] = {
            "resource_type": "host",
            "external_id": "esx-01.local",
            "name": "esx-01.local",
            "payload": {"datastores": ["tstds"], "networks": ["VM Network", "Recovery Network"]},
            "discovered_at": datetime.now(timezone.utc).isoformat(),
        }
        manifest_path = f"{tmpdir}/manifest.json"
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "vm_name": "tstvm",
                    "vm_external_id": "vm-1",
                    "vm_config": {
                        "name": "tstvm",
                        "network_adapters": [{"label": "Network adapter 1", "network": "VM Network", "connected": True}],
                    },
                    "disks": [{"disk_label": "Hard disk 1", "capacity_gb": 40, "datastore_path": "[tstds] tstvm/tstvm.vmdk"}],
                },
                handle,
            )
        BACKUPS[point_id] = abr_v41.normalize_backup_point(
            {
                "id": point_id,
                "chain_id": f"backup-{point_id}",
                "vm_id": "vm-1",
                "vm_name": "tstvm",
                "inventory_id": inventory["id"],
                "inventory_name": inventory["name"],
                "repository_id": "repo-1",
                "repository_name": "Repo",
                "execution_type": "ActiveFull",
                "status": "available",
                "artifact": {"manifest_path": manifest_path, "backup_root": tmpdir},
                "placements": [{"repository_id": "repo-1", "repository_name": "Repo", "manifest_path": manifest_path, "backup_root": tmpdir}],
            }
        )
        return inventory

    def test_restore_start_rejects_a_typo_in_the_legacy_network_map_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            inventory = self._single_nic_network_map_fixture("p-legacy-typo", tmpdir)

            with self.assertRaises(ApiError) as ctx:
                abr_v41.restore_start(
                    {
                        "restore_point_id": "p-legacy-typo",
                        "target": {
                            "inventory_id": inventory["id"],
                            "host_id": "esx-01.local",
                            "datastore_id": f"{inventory['id']}::datastore::tstds",
                            "vm_name": "restoredtstvm",
                        },
                        "network_map": ["no-such-source:Recovery Network"],
                    }
                )

        self.assertEqual(ctx.exception.code, "network_adapter_not_found")
        # 404 is what the CLI maps to exit code 3 (see cli_error_from_exception).
        self.assertEqual(ctx.exception.status_code, 404)
        # A pre-flight check that lets the flight start anyway is not one:
        # the failed validation must not have created a restore session.
        self.assertEqual(TASKS, {})

    def test_restore_start_accepts_a_correctly_spelled_legacy_network_map_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            inventory = self._single_nic_network_map_fixture("p-legacy-ok", tmpdir)

            session = abr_v41.restore_start(
                {
                    "restore_point_id": "p-legacy-ok",
                    "target": {
                        "inventory_id": inventory["id"],
                        "host_id": "esx-01.local",
                        "datastore_id": f"{inventory['id']}::datastore::tstds",
                        "vm_name": "restoredtstvm",
                    },
                    "network_map": ["VM Network:Recovery Network"],
                }
            )

        task = TASKS[session["id"]]
        self.assertEqual(task["parameters"]["network_map"], {"VM Network": "Recovery Network"})

    def test_restore_start_rejects_a_legacy_network_map_that_leaves_a_nic_unmapped(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            inventory = abr_v41.create_inventory_record(
                {"name": "vc-legacy-partial", "type": "vsphere", "address": "192.168.12.39", "username": "root", "password": "secret"}
            )
            RESOURCES_BY_HV[inventory["id"]][("host", "esx-01.local")] = {
                "resource_type": "host",
                "external_id": "esx-01.local",
                "name": "esx-01.local",
                "payload": {"datastores": ["tstds"], "networks": ["VM Network", "DMZ", "Recovery Network"]},
                "discovered_at": datetime.now(timezone.utc).isoformat(),
            }
            manifest_path = f"{tmpdir}/manifest.json"
            with open(manifest_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "vm_name": "tstvm",
                        "vm_external_id": "vm-1",
                        "vm_config": {
                            "name": "tstvm",
                            "network_adapters": [
                                {"label": "Network adapter 1", "network": "VM Network", "connected": True},
                                {"label": "Network adapter 2", "network": "DMZ", "connected": True},
                            ],
                        },
                        "disks": [{"disk_label": "Hard disk 1", "capacity_gb": 40, "datastore_path": "[tstds] tstvm/tstvm.vmdk"}],
                    },
                    handle,
                )
            BACKUPS["p-legacy-partial"] = abr_v41.normalize_backup_point(
                {
                    "id": "p-legacy-partial",
                    "chain_id": "backup-p-legacy-partial",
                    "vm_id": "vm-1",
                    "vm_name": "tstvm",
                    "inventory_id": inventory["id"],
                    "inventory_name": inventory["name"],
                    "repository_id": "repo-1",
                    "repository_name": "Repo",
                    "execution_type": "ActiveFull",
                    "status": "available",
                    "artifact": {"manifest_path": manifest_path, "backup_root": tmpdir},
                    "placements": [{"repository_id": "repo-1", "repository_name": "Repo", "manifest_path": manifest_path, "backup_root": tmpdir}],
                }
            )

            with self.assertRaises(ApiError) as ctx:
                abr_v41.restore_start(
                    {
                        "restore_point_id": "p-legacy-partial",
                        "target": {
                            "inventory_id": inventory["id"],
                            "host_id": "esx-01.local",
                            "datastore_id": f"{inventory['id']}::datastore::tstds",
                            "vm_name": "restoredtstvm",
                        },
                        "network_map": ["VM Network:Recovery Network"],
                    }
                )

        self.assertEqual(ctx.exception.code, "invalid_restore")

    def _two_host_network_inventory(self, point_id: str, tmpdir: str, *, source_host: str) -> dict[str, object]:
        """Inventory with two hosts whose virtual switches differ, plus a restore
        point whose VM has one NIC on the shared network and one on a switch that
        only exists on esx-01."""
        inventory = abr_v41.create_inventory_record(
            {
                "name": "vc-host-networks",
                "type": "vsphere",
                "address": "192.168.13.52",
                "username": "root",
                "password": "secret",
            }
        )
        for host_name, networks in (
            ("esx-01.local", ["VM Network", "DMZ"]),
            ("esx-02.local", ["VM Network", "BMC_Network"]),
        ):
            RESOURCES_BY_HV[inventory["id"]][("host", host_name)] = {
                "resource_type": "host",
                "external_id": host_name,
                "name": host_name,
                "payload": {"datastores": ["tstds"], "networks": networks},
                "discovered_at": datetime.now(timezone.utc).isoformat(),
            }

        manifest_path = f"{tmpdir}/manifest.json"
        with open(manifest_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "vm_name": "tstvm",
                    "vm_external_id": "vm-1",
                    "vm_config": {
                        "name": "tstvm",
                        "source_host": source_host,
                        "network_adapters": [
                            {"label": "Network adapter 1", "network": "VM Network", "connected": True},
                            {"label": "Network adapter 2", "network": "DMZ", "connected": True},
                        ],
                    },
                    "disks": [{"disk_label": "Hard disk 1", "capacity_gb": 40, "datastore_path": "[tstds] tstvm/tstvm.vmdk"}],
                },
                handle,
            )
        BACKUPS[point_id] = abr_v41.normalize_backup_point(
            {
                "id": point_id,
                "chain_id": "backup-host-networks",
                "vm_id": "vm-1",
                "vm_name": "tstvm",
                "inventory_id": inventory["id"],
                "inventory_name": inventory["name"],
                "repository_id": "repo-1",
                "repository_name": "Repo",
                "execution_type": "Incremental",
                "status": "available",
                "artifact": {"manifest_path": manifest_path, "backup_root": tmpdir},
                "placements": [{"repository_id": "repo-1", "repository_name": "Repo", "manifest_path": manifest_path, "backup_root": tmpdir}],
            }
        )
        return inventory

    def test_restore_context_scopes_target_networks_to_the_hosts_that_expose_them(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            inventory = self._two_host_network_inventory("p-hostnet", tmpdir, source_host="esx-02.local")
            context = abr_v41.get_restore_context("p-hostnet")

        networks = {str(item["name"]): item for item in context["targets"]["networks"]}
        self.assertEqual(networks["VM Network"]["host_ids"], ["esx-01.local", "esx-02.local"])
        self.assertEqual(networks["DMZ"]["host_ids"], ["esx-01.local"])
        self.assertEqual(networks["BMC_Network"]["host_ids"], ["esx-02.local"])

        # The default host is esx-02, so only the network it actually exposes may
        # be pre-paired — DMZ lives on esx-01's switch and stays unmapped.
        self.assertEqual(context["defaults"]["target_host_id"], "esx-02.local")
        mappings = context["defaults"]["network_mappings"]
        self.assertEqual(
            [item["target_network_id"] for item in mappings],
            [f"{inventory['id']}::network::VM Network", None],
        )

    def test_restore_start_rejects_target_network_absent_from_selected_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            inventory = self._two_host_network_inventory("p-hostnet-start", tmpdir, source_host="esx-01.local")
            source_nics = abr_v41.get_restore_context("p-hostnet-start")["source_vm"]["nics"]

            with self.assertRaises(ApiError) as ctx:
                abr_v41.restore_start(
                    {
                        "restore_point_id": "p-hostnet-start",
                        "target": {
                            "inventory_id": inventory["id"],
                            "host_id": "esx-02.local",
                            "datastore_id": f"{inventory['id']}::datastore::tstds",
                            "vm_name": "restoredtstvm",
                        },
                        "network_mappings": [
                            {"source_nic_id": source_nics[0]["id"], "target_network_id": f"{inventory['id']}::network::VM Network"},
                            # DMZ exists in the inventory, but only on esx-01.
                            {"source_nic_id": source_nics[1]["id"], "target_network_id": f"{inventory['id']}::network::DMZ"},
                        ],
                    }
                )

        self.assertEqual(ctx.exception.code, "network_not_found")
        self.assertIn("not available on the selected host", ctx.exception.message)
        self.assertEqual(ctx.exception.details.get("host"), "esx-02.local")

    def test_restore_start_accepts_target_networks_present_on_selected_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            inventory = self._two_host_network_inventory("p-hostnet-ok", tmpdir, source_host="esx-01.local")
            source_nics = abr_v41.get_restore_context("p-hostnet-ok")["source_vm"]["nics"]

            session = abr_v41.restore_start(
                {
                    "restore_point_id": "p-hostnet-ok",
                    "target": {
                        "inventory_id": inventory["id"],
                        "host_id": "esx-02.local",
                        "datastore_id": f"{inventory['id']}::datastore::tstds",
                        "vm_name": "restoredtstvm",
                    },
                    "network_mappings": [
                        {"source_nic_id": source_nics[0]["id"], "target_network_id": f"{inventory['id']}::network::VM Network"},
                        {"source_nic_id": source_nics[1]["id"], "target_network_id": f"{inventory['id']}::network::BMC_Network"},
                    ],
                }
            )

        self.assertEqual(
            TASKS[session["id"]]["parameters"]["network_map"],
            {"VM Network": "VM Network", "DMZ": "BMC_Network"},
        )

    def test_restore_start_marks_imported_points_as_having_no_discoverable_source_resource(self) -> None:
        inventory = abr_v41.create_inventory_record(
            {
                "name": "esxi-imported-target",
                "type": "vsphere",
                "address": "192.168.12.38",
                "username": "root",
                "password": "secret",
            }
        )
        RESOURCES_BY_HV[inventory["id"]][("host", "esx-02.local")] = {
            "resource_type": "host",
            "external_id": "esx-02.local",
            "name": "esx-02.local",
            "payload": {"datastores": ["tstds"]},
            "discovered_at": datetime.now(timezone.utc).isoformat(),
        }
        imported_point = abr_v41.normalize_backup_point(
            {
                "id": "rp-imported",
                "job_id": None,
                "job_name": "Imported Backup",
                "vm_id": "imported-22222222-2222-2222-2222-222222222222",
                "vm_name": "imported-vm",
                "inventory_id": None,
                "inventory_name": None,
                "repository_id": "repo-1",
                "repository_name": "Repo",
                "execution_type": "ActiveFull",
                "created_at": "2026-05-25T20:00:00+00:00",
                "artifact": {"manifest_path": "/tmp/backups/imported-vm/manifest.json", "backup_root": "/tmp/backups/imported-vm"},
                "placements": [{"repository_id": "repo-1", "repository_name": "Repo", "manifest_path": "/tmp/backups/imported-vm/manifest.json", "backup_root": "/tmp/backups/imported-vm"}],
                "status": "available",
            }
        )
        BACKUPS["rp-imported"] = imported_point
        self.assertEqual(imported_point.get("inventory_id"), "", "sanity check: import_backup_chain-style points normalize to an empty inventory_id")

        session = abr_v41.restore_start(
            {
                "point_id": "rp-imported",
                "name": "restored-imported-vm",
                "target_inventory_id": inventory["id"],
                "target_host": "192.168.12.38",
                "target_datastore": "tstds",
            }
        )

        self.assertEqual(TASKS[session["id"]]["source_resource_available"], False)

        BACKUPS["rp-native"] = abr_v41.normalize_backup_point(
            {
                "id": "rp-native",
                "vm_id": "vm-native-1",
                "vm_name": "nativevm",
                "inventory_id": "src-inv-native",
                "inventory_name": "src",
                "repository_id": "repo-1",
                "repository_name": "Repo",
                "execution_type": "ActiveFull",
                "created_at": "2026-05-25T20:00:00+00:00",
                "artifact": {"manifest_path": "/tmp/backups/nativevm/manifest.json", "backup_root": "/tmp/backups/nativevm"},
                "placements": [{"repository_id": "repo-1", "repository_name": "Repo", "manifest_path": "/tmp/backups/nativevm/manifest.json", "backup_root": "/tmp/backups/nativevm"}],
                "status": "available",
            }
        )
        native_session = abr_v41.restore_start(
            {
                "point_id": "rp-native",
                "name": "restored-native-vm",
                "target_inventory_id": inventory["id"],
                "target_host": "192.168.12.38",
                "target_datastore": "tstds",
            }
        )
        self.assertEqual(TASKS[native_session["id"]]["source_resource_available"], True)


class SystemInfoTests(unittest.TestCase):
    """Guards against system_info() regressing to the hardcoded/zeroed stand-in
    the About tab used to render (hostname "altastor", 0 cores, 0d0h0m, ...)."""

    def test_system_info_reports_live_host_facts_not_placeholders(self) -> None:
        info = abr_v41.system_info()

        self.assertEqual(info["hostname"], socket.gethostname())
        self.assertNotEqual(info["hostname"], "altastor")
        self.assertTrue(info["os"])
        self.assertTrue(info["kernel"])
        self.assertIsInstance(info["cpu_cores"], int)
        self.assertGreater(info["cpu_cores"], 0)
        self.assertIsInstance(info["uptime_sec"], float)
        self.assertGreaterEqual(info["uptime_sec"], 0)
        self.assertIsInstance(info["memory_gb"], float)
        self.assertGreater(info["memory_gb"], 0)


if __name__ == "__main__":
    unittest.main()
