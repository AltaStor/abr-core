# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import io
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

os.environ["ABR_ENABLE_MOCK_INVENTORY"] = "1"

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError:
    repo_root = pathlib.Path(__file__).resolve().parents[3]
    venv_lib = repo_root / ".venv" / "lib"
    for site_packages in sorted(venv_lib.glob("python*/site-packages")):
        sys.path.insert(0, str(site_packages))
    from fastapi.testclient import TestClient

from python.cli.commands.base import CommandDependencies
from python.cli.commands.inventory_commands import InventoryCommands
from python.cli.commands.job_commands import JobCommands
from python.cli.commands.repository_commands import RepositoryCommands
from python.cli.parsing.parser_builder import build_parser
from python.srv.api.auth_guard import require_session
from python.srv.backup_server import app
from python.srv.core.mongo_db_wrapper import BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS


class CliRedesignTests(unittest.TestCase):
    def test_root_help_omits_removed_command_groups(self) -> None:
        parser = build_parser()
        help_output = parser.format_help()

        self.assertNotIn("license", help_output)
        self.assertNotIn("security", help_output)

    def test_group_help_omits_removed_subcommands(self) -> None:
        parser = build_parser()

        inventory_help = self._parse_help(parser, ["inventory", "--help"])
        repo_help = self._parse_help(parser, ["repo", "--help"])
        job_help = self._parse_help(parser, ["job", "--help"])
        restore_help = self._parse_help(parser, ["restore", "--help"])
        system_help = self._parse_help(parser, ["system", "--help"])

        self.assertIn("list-vms", inventory_help)
        self.assertNotIn("create", repo_help)
        self.assertNotIn("run", job_help)
        self.assertNotIn("stop", job_help)
        for removed_restore_command in (
            "vm",
            "instant-vm",
            "instant-disk",
            "disk",
            "publish-disk",
            "unpublish-disk",
            "migrate",
            "finalize",
            "stop",
        ):
            self.assertNotIn(removed_restore_command, restore_help)
        self.assertNotIn("update", system_help)
        self.assertNotIn("logs", system_help)

    def test_removed_commands_fail_as_unknown_commands(self) -> None:
        parser = build_parser()

        removed_commands = [
            ["inventory", "vms"],
            ["repo", "create"],
            ["job", "run"],
            ["job", "stop"],
            ["backup", "points"],
            ["backup", "export"],
            ["restore", "vm"],
            ["restore", "instant-vm"],
            ["restore", "instant-disk"],
            ["restore", "disk"],
            ["restore", "publish-disk"],
            ["restore", "unpublish-disk"],
            ["restore", "migrate"],
            ["restore", "finalize"],
            ["restore", "stop"],
            ["license"],
            ["security"],
            ["system", "logs"],
            ["system", "update"],
            ["session", "logs"],
        ]

        for argv in removed_commands:
            with self.subTest(argv=argv):
                error_output = self._parse_error(parser, argv)
                self.assertIn("invalid choice", error_output)

    def test_session_log_replaces_session_logs(self) -> None:
        parser = build_parser()

        args = parser.parse_args(["session", "log", "--id", "sess-1"])
        self.assertEqual(args.command_group, "session")
        self.assertEqual(args.session_command, "log")

        error_output = self._parse_error(parser, ["session", "logs", "--id", "sess-1"])
        self.assertIn("invalid choice", error_output)

    def test_parser_accepts_new_job_create_shape(self) -> None:
        args = build_parser().parse_args(
            [
                "job",
                "create",
                "--name",
                "Job-SQL",
                "--inventory",
                "Prod-vCenter",
                "--objects",
                "VM-SQL-01",
                "--repo",
                "Local-Disk",
                "--retention-days",
                "7",
            ]
        )

        self.assertEqual(args.command_group, "job")
        self.assertEqual(args.job_command, "create")
        self.assertEqual(args.objects, "VM-SQL-01")
        self.assertEqual(args.retention_days, 7)

    def test_parser_accepts_job_create_with_inventory_and_repo_ids(self) -> None:
        args = build_parser().parse_args(
            [
                "job",
                "create",
                "--name",
                "Job-SQL",
                "--inventory-id",
                "hv-101",
                "--objects",
                "VM-SQL-01,VM-WEB-01",
                "--repo-id",
                "repo-202",
                "--retention-days",
                "30",
            ]
        )

        self.assertEqual(args.inventory_id, "hv-101")
        self.assertEqual(args.repo_id, "repo-202")
        self.assertEqual(args.objects, "VM-SQL-01,VM-WEB-01")
        self.assertEqual(args.retention_days, 30)

    def test_job_create_help_omits_removed_flags(self) -> None:
        parser = build_parser()

        help_output = self._parse_help(parser, ["job", "create", "--help"])

        self.assertNotIn("--mode", help_output)
        self.assertNotIn("--schedule-daily", help_output)
        self.assertNotIn("--gfs-policy", help_output)

    def test_job_create_copy_help_omits_removed_mode_flag(self) -> None:
        parser = build_parser()

        help_output = self._parse_help(parser, ["job", "create-copy", "--help"])

        self.assertNotIn("--mode", help_output)

    def test_parser_accepts_all_job_schedule_types(self) -> None:
        parser = build_parser()

        def _create_args(*extra: str) -> object:
            return parser.parse_args(
                ["job", "create", "--name", "Job-SQL", "--vm", "vm-1", "--repo", "Local-Disk", *extra]
            )

        daily_args = _create_args("--schedule", "daily", "--time", "02:00")
        weekly_args = _create_args("--schedule", "weekly", "--days", "Mon,Fri", "--time", "03:00")
        monthly_args = _create_args("--schedule", "monthly", "--day-of-month", "15", "--time", "04:00")
        periodic_args = _create_args("--schedule", "periodic", "--every", "30m")

        self.assertEqual(daily_args.schedule, "daily")
        self.assertEqual(daily_args.time, "02:00")
        self.assertEqual(weekly_args.days, ["monday", "friday"])
        self.assertEqual(monthly_args.day_of_month, 15)
        self.assertEqual(periodic_args.every, "30m")

    def test_job_start_help_mentions_full_and_examples(self) -> None:
        parser = build_parser()

        help_output = self._parse_help(parser, ["job", "start", "--help"])

        self.assertIn("[--full | --synthetic]", help_output)
        self.assertIn("Force a full backup", help_output)
        self.assertIn("merging the stored chain", help_output)
        self.assertIn('abr job start --name "Job-SQL-Server" --full', help_output)
        self.assertIn("abr job start --id", help_output)
        self.assertIn("<job-id> --full", help_output)

    def test_parser_accepts_job_start_full_by_name_and_id(self) -> None:
        parser = build_parser()

        by_name = parser.parse_args(["job", "start", "--name", "Job-SQL-Server", "--full"])
        by_id = parser.parse_args(["job", "start", "--id", "job-1", "--full"])

        self.assertTrue(by_name.full)
        self.assertEqual(by_name.name, "Job-SQL-Server")
        self.assertTrue(by_id.full)
        self.assertEqual(by_id.id, "job-1")

    def test_inventory_repo_and_job_commands_accept_name_or_id_selectors(self) -> None:
        parser = build_parser()

        cases = (
            (["inventory", "info", "--id", "hv-1"], "id", "hv-1"),
            (["inventory", "rescan", "--name", "Prod-vCenter"], "name", "Prod-vCenter"),
            (["inventory", "test", "--id", "hv-2"], "id", "hv-2"),
            (["repo", "info", "--id", "repo-1"], "id", "repo-1"),
            (["repo", "edit", "--name", "Local-Storage"], "name", "Local-Storage"),
            (["repo", "edit", "--id", "repo-2", "--name", "Archive-2"], "id", "repo-2"),
            (["repo", "remove", "--id", "repo-3"], "id", "repo-3"),
            (["repo", "verify", "--name", "Archive"], "name", "Archive"),
            (["job", "info", "--id", "job-1"], "id", "job-1"),
            (["job", "edit", "--name", "Daily Backup"], "name", "Daily Backup"),
            (["job", "delete", "--id", "job-2"], "id", "job-2"),
            (["job", "start", "--name", "Daily Backup"], "name", "Daily Backup"),
        )

        for argv, attr, expected in cases:
            with self.subTest(argv=argv):
                args = parser.parse_args(argv)
                self.assertEqual(getattr(args, attr), expected)

    def test_selector_flags_are_optional_at_parse_time(self) -> None:
        parser = build_parser()

        cases = (
            ["inventory", "info"],
            ["inventory", "edit", "--name", "Prod-vCenter", "--id", "hv-1"],
            ["repo", "info"],
            ["repo", "remove", "--name", "Archive", "--id", "repo-1"],
            ["job", "info"],
            ["job", "start", "--name", "Daily Backup", "--id", "job-1"],
        )

        for argv in cases:
            with self.subTest(argv=argv):
                args = parser.parse_args(argv)
                if "--name" in argv:
                    self.assertIsNotNone(args.name)
                else:
                    self.assertIsNone(getattr(args, "name", None))

    def test_selector_resolution_requires_name_or_id_and_detects_mismatch(self) -> None:
        cases = (
            (RepositoryCommands, "api/v1/repositories"),
            (InventoryCommands, "api/v1/inventories"),
            (JobCommands, "api/v1/jobs"),
        )

        for command_cls, list_path in cases:
            with self.subTest(command=command_cls.__name__):

                def fake_get(path: str, params=None, timeout=None, _list_path=list_path) -> dict:
                    self.assertEqual(path, _list_path)
                    return {"items": [{"id": "rec-1", "name": "Rec-One"}]}

                with patch("python.cli.api.client.AltaStorApiClient.get", side_effect=fake_get):
                    deps = CommandDependencies(
                        SimpleNamespace(base_url="http://127.0.0.1:8000", output="table")
                    )

                    with self.assertRaises(ValueError) as missing:
                        command_cls._resolve_id(deps, SimpleNamespace(name=None, id=None))
                    self.assertIn("requires --name or --id", str(missing.exception))

                    resolved = command_cls._resolve_id(deps, SimpleNamespace(name="Rec-One", id="rec-1"))
                    self.assertEqual(resolved, "rec-1")

                    with self.assertRaises(ValueError) as mismatch:
                        command_cls._resolve_id(deps, SimpleNamespace(name="Rec-One", id="other-id"))
                    self.assertIn("selector mismatch", str(mismatch.exception).lower())

                    with self.assertRaises(ValueError) as not_found:
                        command_cls._resolve_id(deps, SimpleNamespace(name="Unknown", id=None))
                    self.assertIn("not found", str(not_found.exception).lower())

    def test_repo_add_and_edit_help_expose_immutability_flags(self) -> None:
        parser = build_parser()

        add_help = self._parse_help(parser, ["repo", "add", "--help"])
        edit_help = self._parse_help(parser, ["repo", "edit", "--help"])
        repo_help = self._parse_help(parser, ["repo", "--help"])

        self.assertIn("--immutable {none,governance,compliance}", add_help)
        self.assertIn("--immutability-days", add_help)
        self.assertIn("--immutable {none,governance,compliance}", edit_help)
        self.assertIn("--immutability-days", edit_help)
        self.assertIn("verify-immutability", repo_help)
        self.assertIn("remove", repo_help)
        self.assertIn("--username", add_help)
        self.assertIn("--password", add_help)
        self.assertIn("--description", edit_help)

    def test_job_create_schedule_validates_time_and_weekdays(self) -> None:
        parser = build_parser()

        invalid_time = self._parse_error(
            parser,
            [
                "job", "create", "--name", "Job-SQL", "--vm", "vm-1", "--repo", "Local-Disk",
                "--schedule", "daily", "--time", "2:00",
            ],
        )
        invalid_weekday = self._parse_error(
            parser,
            [
                "job", "create", "--name", "Job-SQL", "--vm", "vm-1", "--repo", "Local-Disk",
                "--schedule", "weekly", "--days", "Mon,Funday", "--time", "02:00",
            ],
        )

        invalid_synthetic_full_days = self._parse_error(
            parser,
            [
                "job", "create", "--name", "Job-SQL", "--vm", "vm-1", "--repo", "Local-Disk",
                "--schedule", "daily", "--time", "02:00", "--synthetic-full-days", "funday",
            ],
        )
        invalid_active_full_days = self._parse_error(
            parser,
            [
                "job", "create", "--name", "Job-SQL", "--vm", "vm-1", "--repo", "Local-Disk",
                "--schedule", "daily", "--time", "02:00", "--active-full-days", "funday",
            ],
        )
        invalid_edit_synthetic_full_days = self._parse_error(
            parser,
            ["job", "edit", "--name", "Job-SQL", "--synthetic-full-days", "funday"],
        )

        self.assertIn("HH:MM format", invalid_time)
        self.assertIn("Unknown weekday value", invalid_weekday)
        self.assertIn("Unknown weekday value", invalid_synthetic_full_days)
        self.assertIn("Unknown weekday value", invalid_active_full_days)
        self.assertIn("Unknown weekday value", invalid_edit_synthetic_full_days)

    def test_parser_accepts_job_create_copy_and_restore_commands(self) -> None:
        parser = build_parser()

        copy_args = parser.parse_args(
            [
                "job",
                "create-copy",
                "--name",
                "Copy-to-NFS",
                "--source",
                "Job-SQL",
                "--repo",
                "NFS-Target",
            ]
        )
        restore_list_args = parser.parse_args(["restore", "list", "--vm", "VM-SQL-01", "--backup", "backup-1"])
        restore_start_args = parser.parse_args(
            [
                "restore",
                "start",
                "--restore-point",
                "rp-101",
                "--target-inventory",
                "DR-Host",
                "--target-host",
                "esx-01",
                "--datastore",
                "Local-Storage-DR",
                "--new-name",
                "VM-SQL-01-restored",
                "--power-on",
            ]
        )

        self.assertEqual(copy_args.job_command, "create-copy")
        self.assertEqual(copy_args.source_job, "Job-SQL")
        self.assertEqual(copy_args.repo, "NFS-Target")
        self.assertEqual(restore_list_args.restore_command, "list")
        self.assertEqual(restore_list_args.vm, "VM-SQL-01")
        self.assertEqual(restore_list_args.backup, "backup-1")
        self.assertEqual(restore_start_args.restore_command, "start")
        self.assertEqual(restore_start_args.restore_point, "rp-101")
        self.assertTrue(restore_start_args.power_on)

    def test_parser_accepts_backup_management_commands_and_id_filters(self) -> None:
        parser = build_parser()

        list_args = parser.parse_args(["backup", "list", "--repo", "repo-1"])
        verify_args = parser.parse_args(["backup", "verify", "--id", "backup-1"])
        delete_args = parser.parse_args(["backup", "delete", "--id", "backup-1"])
        import_args = parser.parse_args(["backup", "import", "--path", "/mnt/external/backup", "--repo", "repo-1"])

        self.assertEqual(list_args.repo, "repo-1")
        self.assertEqual(verify_args.backup_command, "verify")
        self.assertEqual(verify_args.id, "backup-1")
        self.assertEqual(delete_args.backup_command, "delete")
        self.assertEqual(delete_args.id, "backup-1")
        self.assertEqual(import_args.backup_command, "import")
        self.assertEqual(import_args.path, "/mnt/external/backup")

    def test_parser_accepts_global_output_after_command_path(self) -> None:
        parser = build_parser()

        backup_args = parser.parse_args(["backup", "list", "--output", "table"])
        restore_args = parser.parse_args(["restore", "list", "--vm", "tstvm", "--output", "table"])

        self.assertEqual(backup_args.command_group, "backup")
        self.assertEqual(backup_args.backup_command, "list")
        self.assertEqual(backup_args.output, "table")

        self.assertEqual(restore_args.command_group, "restore")
        self.assertEqual(restore_args.restore_command, "list")
        self.assertEqual(restore_args.vm, "tstvm")
        self.assertEqual(restore_args.output, "table")

    def test_parser_defaults_output_to_table_and_allows_json_override(self) -> None:
        parser = build_parser()

        default_args = parser.parse_args(["backup", "list"])
        json_args = parser.parse_args(["backup", "list", "--output", "json"])

        self.assertEqual(default_args.output, "table")
        self.assertEqual(json_args.output, "json")

    def test_parser_accepts_global_base_url_after_command_path(self) -> None:
        parser = build_parser()

        args = parser.parse_args(["system", "health", "--base-url", "http://127.0.0.1:8001"])

        self.assertEqual(args.command_group, "system")
        self.assertEqual(args.system_command, "health")
        self.assertEqual(args.base_url, "http://127.0.0.1:8001")

    def _parse_help(self, parser: object, argv: list[str]) -> str:
        buffer = io.StringIO()
        with redirect_stdout(buffer), self.assertRaises(SystemExit) as exc:
            parser.parse_args(argv)
        self.assertEqual(exc.exception.code, 0)
        return buffer.getvalue()

    def _parse_error(self, parser: object, argv: list[str]) -> str:
        buffer = io.StringIO()
        with redirect_stderr(buffer), self.assertRaises(SystemExit) as exc:
            parser.parse_args(argv)
        self.assertEqual(exc.exception.code, 2)
        return buffer.getvalue()


class RepositoryApiEditTests(unittest.TestCase):
    def setUp(self) -> None:
        HYPERVISORS.clear()
        RESOURCES_BY_HV.clear()
        TASKS.clear()
        JOBS.clear()
        REPOSITORIES.clear()
        BACKUPS.clear()
        app.dependency_overrides[require_session] = lambda: {"user_id": "test-user", "user_name": "test-user"}
        self.client_cm = TestClient(app)
        self.client = self.client_cm.__enter__()
        self._repo_root = tempfile.mkdtemp(prefix="abr-test-repos-")

    def tearDown(self) -> None:
        self.client_cm.__exit__(None, None, None)
        app.dependency_overrides.pop(require_session, None)
        HYPERVISORS.clear()
        RESOURCES_BY_HV.clear()
        TASKS.clear()
        JOBS.clear()
        REPOSITORIES.clear()
        BACKUPS.clear()
        shutil.rmtree(self._repo_root, ignore_errors=True)

    def test_patch_repository_updates_local_path(self) -> None:
        initial_path = os.path.join(self._repo_root, "backup")
        os.makedirs(initial_path, exist_ok=True)
        created = self.client.post(
            "/api/v1/repositories",
            json={"name": "local1", "type": "local", "path": initial_path, "metadata": {}},
        )
        self.assertEqual(created.status_code, 200)
        repository = created.json()

        moved_path = os.path.join(self._repo_root, "backup-new")
        os.makedirs(moved_path, exist_ok=True)
        os.chmod(moved_path, 0o750)
        updated = self.client.patch(
            f"/api/v1/repositories/{repository['id']}",
            json={"path": moved_path, "metadata": {}},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["path"], moved_path)

    def test_delete_repository_with_unknown_id_returns_404(self) -> None:
        response = self.client.delete("/api/v1/repositories/00000000-0000-0000-0000-000000000000")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "repository_not_found")

    def test_delete_inventory_with_unknown_id_returns_404(self) -> None:
        response = self.client.delete("/api/v1/inventories/00000000-0000-0000-0000-000000000000")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "inventory_not_found")


class JobApiEditTests(unittest.TestCase):
    def setUp(self) -> None:
        HYPERVISORS.clear()
        RESOURCES_BY_HV.clear()
        TASKS.clear()
        JOBS.clear()
        REPOSITORIES.clear()
        BACKUPS.clear()
        app.dependency_overrides[require_session] = lambda: {"user_id": "test-user", "user_name": "test-user"}
        self.client_cm = TestClient(app)
        self.client = self.client_cm.__enter__()
        self._repo_root = tempfile.mkdtemp(prefix="abr-test-repos-")

    def tearDown(self) -> None:
        self.client_cm.__exit__(None, None, None)
        app.dependency_overrides.pop(require_session, None)
        HYPERVISORS.clear()
        RESOURCES_BY_HV.clear()
        TASKS.clear()
        JOBS.clear()
        REPOSITORIES.clear()
        BACKUPS.clear()
        shutil.rmtree(self._repo_root, ignore_errors=True)

    def _create_inventory(self, name: str) -> dict:
        created = self.client.post(
            "/api/v1/inventories",
            json={"name": name, "type": "mock", "address": f"mock://{name}", "username": "mock", "password": "mock"},
        )
        self.assertEqual(created.status_code, 200, created.text)
        inventory = created.json()
        rescan = self.client.post(f"/api/v1/inventories/{inventory['id']}/rescan")
        self.assertEqual(rescan.status_code, 200, rescan.text)
        return inventory

    def _create_repository(self, name: str, path: str) -> dict:
        real_path = os.path.join(self._repo_root, path.lstrip("/"))
        os.makedirs(real_path, exist_ok=True)
        created = self.client.post(
            "/api/v1/repositories",
            json={"name": name, "type": "local", "path": real_path, "metadata": {}, "locations": []},
        )
        self.assertEqual(created.status_code, 200, created.text)
        return created.json()

    def test_patch_job_updates_schedule_and_metadata(self) -> None:
        inventory = self._create_inventory("inv-schedule")
        repository = self._create_repository("repo-schedule", "/backup/schedule")

        created = self.client.post(
            "/api/v1/jobs",
            json={
                "name": "Job-SQL-Server",
                "vms": ["vm-1"],
                "inventory_id": inventory["id"],
                "repo_id": repository["id"],
                "schedule": {"type": "daily", "time": "01:00", "enabled": True},
                "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"},
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        job = created.json()
        self.assertEqual(job["schedule"]["type"], "daily")

        updated_schedule = self.client.patch(
            f"/api/v1/jobs/{job['id']}",
            json={"schedule": {"type": "weekly", "days": ["monday", "friday"], "time": "02:00", "enabled": False}},
        )
        self.assertEqual(updated_schedule.status_code, 200, updated_schedule.text)
        self.assertEqual(updated_schedule.json()["schedule"]["type"], "weekly")
        self.assertEqual(updated_schedule.json()["schedule"]["days"], ["monday", "friday"])
        self.assertEqual(updated_schedule.json()["status"], "disabled")

        updated_name = self.client.patch(
            f"/api/v1/jobs/{job['id']}",
            json={"name": "Job-SQL-Server-Renamed"},
        )
        self.assertEqual(updated_name.status_code, 200, updated_name.text)
        self.assertEqual(updated_name.json()["name"], "Job-SQL-Server-Renamed")

    def test_patch_job_updates_inventory(self) -> None:
        inventory_a = self._create_inventory("inv-a")
        inventory_b = self._create_inventory("inv-b")
        repository = self._create_repository("repo-inventory-switch", "/backup/inventory-switch")

        created = self.client.post(
            "/api/v1/jobs",
            json={
                "name": "Job-SQL-Server",
                "vms": ["vm-1"],
                "inventory_id": inventory_a["id"],
                "repo_id": repository["id"],
                "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"},
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        job = created.json()
        self.assertEqual(job["inventory"]["id"], inventory_a["id"])

        updated = self.client.patch(
            f"/api/v1/jobs/{job['id']}",
            json={"inventory_id": inventory_b["id"], "vms": ["vm-1"]},
        )

        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["inventory"]["id"], inventory_b["id"])

    def test_delete_job_with_unknown_id_returns_404(self) -> None:
        response = self.client.delete("/api/v1/jobs/00000000-0000-0000-0000-000000000000")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "job_not_found")


class InventoryListVmsCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.command = InventoryCommands()
        self.args = SimpleNamespace(
            name="rec-inv",
            id=None,
            filter=None,
            base_url="http://127.0.0.1:8000",
            output="table",
        )
        self._tree_nodes = [
            {
                "kind": "inventory",
                "label": "rec-inv",
                "inventory_id": "inv-1",
                "children": [
                    {
                        "kind": "cluster",
                        "label": "cluster-a",
                        "inventory_id": "inv-1",
                        "children": [
                            {
                                "kind": "vm",
                                "label": "LNX-EFI-M",
                                "inventory_id": "inv-1",
                                "vm_id": "vm-101",
                                "guest_os": "Debian GNU/Linux 12 (64-bit)",
                                "cpu_count": 1,
                                "memory_mb": 2048,
                                "power_state": "poweredOn",
                                "children": [],
                            },
                            {
                                "kind": "vm",
                                "label": "LNX-EFI-L",
                                "inventory_id": "inv-1",
                                "vm_id": "vm-102",
                                "guest_os": "Debian GNU/Linux 12 (64-bit)",
                                "cpu_count": 2,
                                "memory_mb": 8192,
                                "power_state": "poweredOff",
                                "children": [],
                            },
                        ],
                    },
                ],
            },
            {
                "kind": "inventory",
                "label": "other-inv",
                "inventory_id": "inv-2",
                "children": [
                    {
                        "kind": "vm",
                        "label": "OTHER-VM",
                        "inventory_id": "inv-2",
                        "vm_id": "vm-201",
                        "guest_os": "Windows Server 2022",
                        "cpu_count": 4,
                        "memory_mb": 16384,
                        "power_state": "poweredOn",
                        "children": [],
                    },
                ],
            },
        ]

    def _fake_get(self, path: str, params: dict | None = None, timeout: int | None = None) -> dict:
        if path == "api/v1/inventories":
            return {"items": [{"id": "inv-1", "name": "rec-inv"}, {"id": "inv-2", "name": "other-inv"}]}

        if path == "api/v1/inventories/vms/tree":
            return {"nodes": self._tree_nodes}

        raise AssertionError(f"Unexpected GET: {path}")

    @patch("python.cli.cli_context.CliContext.print_output")
    @patch("python.cli.api.client.AltaStorApiClient.get")
    def test_list_vms_flattens_only_the_selected_inventorys_tree(self, get_mock, print_output_mock) -> None:
        get_mock.side_effect = self._fake_get

        result = self.command.list_vms(self.args)

        self.assertEqual(result, 0)

        payload = print_output_mock.call_args.args[0]
        self.assertEqual(set(payload.keys()), {"items"})
        items = payload["items"]
        names = [vm["label"] for vm in items]

        self.assertEqual(names, ["LNX-EFI-M", "LNX-EFI-L"])
        self.assertTrue(all(vm["inventory_id"] == "inv-1" for vm in items))

        columns = print_output_mock.call_args.args[1]
        self.assertEqual(
            columns,
            [
                ("label", "NAME"),
                ("host_name", "HOST"),
                ("power_state", "POWER STATE"),
                ("guest_os", "GUEST OS"),
                ("cpu_count", "CPU"),
                ("memory_mb", "MEMORY"),
                ("vm_id", "VM ID"),
            ],
        )

    @patch("python.cli.cli_context.CliContext.print_output")
    @patch("python.cli.api.client.AltaStorApiClient.get")
    def test_list_vms_filter_matches_case_insensitive_substring(self, get_mock, print_output_mock) -> None:
        get_mock.side_effect = self._fake_get
        self.args.filter = "-l"

        self.command.list_vms(self.args)

        payload = print_output_mock.call_args.args[0]
        self.assertEqual([vm["label"] for vm in payload["items"]], ["LNX-EFI-L"])

    @patch("python.cli.cli_context.CliContext.print_output")
    @patch("python.cli.api.client.AltaStorApiClient.get")
    def test_list_vms_filter_with_no_matches_prints_empty_list(self, get_mock, print_output_mock) -> None:
        get_mock.side_effect = self._fake_get
        self.args.filter = "does-not-exist"

        self.command.list_vms(self.args)

        payload = print_output_mock.call_args.args[0]
        self.assertEqual(payload, {"items": []})

    @patch("python.cli.cli_context.CliContext.print_output")
    @patch("python.cli.api.client.AltaStorApiClient.get")
    def test_list_vms_by_id_selector(self, get_mock, print_output_mock) -> None:
        get_mock.side_effect = self._fake_get
        self.args.name = None
        self.args.id = "inv-2"

        self.command.list_vms(self.args)

        payload = print_output_mock.call_args.args[0]
        self.assertEqual([vm["label"] for vm in payload["items"]], ["OTHER-VM"])

    @patch("python.cli.api.client.AltaStorApiClient.get")
    def test_list_vms_requires_name_or_id(self, get_mock) -> None:
        self.args.name = None
        self.args.id = None

        with self.assertRaises(ValueError):
            self.command.list_vms(self.args)
        get_mock.assert_not_called()

    @patch("python.cli.api.client.AltaStorApiClient.get")
    def test_list_vms_unknown_inventory_name_raises(self, get_mock) -> None:
        get_mock.side_effect = self._fake_get
        self.args.name = "does-not-exist"

        with self.assertRaises(ValueError):
            self.command.list_vms(self.args)

    @patch("python.cli.api.client.AltaStorApiClient.get")
    def test_list_vms_unknown_inventory_id_raises(self, get_mock) -> None:
        get_mock.side_effect = self._fake_get
        self.args.name = None
        self.args.id = "00000000-0000-0000-0000-000000000000"

        with self.assertRaises(ValueError) as ctx:
            self.command.list_vms(self.args)
        self.assertIn("not found", str(ctx.exception).lower())

    @patch("python.cli.api.client.AltaStorApiClient.get")
    def test_list_vms_malformed_inventory_id_raises(self, get_mock) -> None:
        get_mock.side_effect = self._fake_get
        self.args.name = None
        self.args.id = "not-a-uuid"

        with self.assertRaises(ValueError) as ctx:
            self.command.list_vms(self.args)
        self.assertIn("not found", str(ctx.exception).lower())

    @patch("python.cli.cli_context.CliContext.print_output")
    @patch("python.cli.api.client.AltaStorApiClient.get")
    def test_list_vms_inventory_with_no_discovered_vms_prints_empty_list(self, get_mock, print_output_mock) -> None:
        def fake_get(path: str, params: dict | None = None, timeout: int | None = None) -> dict:
            if path == "api/v1/inventories":
                return {"items": [{"id": "inv-3", "name": "empty-inv"}]}

            if path == "api/v1/inventories/vms/tree":
                return {"nodes": [{"kind": "inventory", "label": "empty-inv", "inventory_id": "inv-3", "children": []}]}
            raise AssertionError(f"Unexpected GET: {path}")


        get_mock.side_effect = fake_get
        self.args.name = "empty-inv"

        self.command.list_vms(self.args)

        payload = print_output_mock.call_args.args[0]
        self.assertEqual(payload, {"items": []})


if __name__ == "__main__":
    unittest.main()
