# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import base64
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from python.cli.commands.backup_commands import BackupCommands
from python.cli.commands.session_commands import SessionCommands
from python.cli.commands.system_commands import PASSWORD_ENV_VAR, SystemCommands
from python.cli.errors import CliStructuredError
from python.cli.exit_codes import INVALID_STATE, INVALID_USAGE
from python.cli.parsing.parser_builder import AltaStorParserBuilder
from python.srv.core.secure_archive import encrypt_payload


class _FakeResponse:
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


class BackupSessionSystemParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = AltaStorParserBuilder().build()

    def test_backup_help_lists_subcommands(self) -> None:
        help_output = self._parse_help(["backup", "--help"])
        for command in ("list", "info", "delete", "verify", "import"):
            self.assertIn(command, help_output)

    def test_session_help_lists_subcommands(self) -> None:
        help_output = self._parse_help(["session", "--help"])
        for command in ("list", "info", "log", "stop"):
            self.assertIn(command, help_output)

    def test_system_help_lists_subcommands(self) -> None:
        help_output = self._parse_help(["system", "--help"])
        for command in ("version", "health", "export", "import", "get-support-bundle"):
            self.assertIn(command, help_output)

    def test_session_list_and_stop_parsers_accept_expected_flags(self) -> None:
        list_args = self.parser.parse_args(["session", "list", "--job", "Nightly"])
        stop_args = self.parser.parse_args(["session", "stop", "--id", "sess-1", "--force"])

        self.assertEqual(list_args.job, "Nightly")
        self.assertEqual(stop_args.id, "sess-1")
        self.assertTrue(stop_args.force)

    def test_get_support_bundle_file_flag_is_optional(self) -> None:
        without_file = self.parser.parse_args(["system", "get-support-bundle"])
        with_file = self.parser.parse_args(["system", "get-support-bundle", "--file", "/tmp/out.zip"])

        self.assertIsNone(without_file.file)
        self.assertEqual(with_file.file, "/tmp/out.zip")

    def test_backup_import_parser_requires_exactly_one_repository_selector(self) -> None:
        stream = io.StringIO()
        with redirect_stderr(stream), self.assertRaises(SystemExit) as ctx:
            self.parser.parse_args(["backup", "import", "--path", "/mnt/external/backup"])
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("one of the arguments --repo --repo-id is required", stream.getvalue())

    def _parse_help(self, argv: list[str]) -> str:
        stream = io.StringIO()
        with redirect_stdout(stream):
            with self.assertRaises(SystemExit):
                self.parser.parse_args(argv)
        return stream.getvalue()


class BackupCommandTests(unittest.TestCase):
    @patch("python.cli.api.client.requests.request")
    def test_list_filters_by_repo_and_prints_table(self, request_mock: object) -> None:
        request_mock.return_value = _FakeResponse(
            {
                "items": [
                    {
                        "id": "backup-1",
                        "name": "Nightly / vm-1 / repo-1",
                        "status": "available",
                        "restore_point_count": 2,
                        "latest_execution_type": "ActiveFull",
                        "last_restore_point_at": "2026-05-26T20:30:00+00:00",
                    }
                ]
            }
        )
        args = SimpleNamespace(base_url="http://example.test", output="table", log_level="info", repo="repo-1")

        stream = io.StringIO()
        with redirect_stdout(stream):
            exit_code = BackupCommands().list(args)

        self.assertEqual(exit_code, 0)
        self.assertIn("backup-1", stream.getvalue())
        request_mock.assert_called_once_with(
            "GET",
            "http://example.test/api/v1/backups",
            json=None,
            params={"repo": "repo-1"},
            timeout=30,
            headers={"X-Client-Type": "cli", "X-ABR-API-Version": "1.0"},
            verify=False,
        )

    @patch("python.cli.api.client.requests.request")
    def test_list_without_repo_sends_no_params(self, request_mock: object) -> None:
        request_mock.return_value = _FakeResponse({"items": []})
        args = SimpleNamespace(base_url="http://example.test", output="json", log_level="info", repo=None)

        with redirect_stdout(io.StringIO()):
            BackupCommands().list(args)

        request_mock.assert_called_once_with(
            "GET",
            "http://example.test/api/v1/backups",
            json=None,
            params=None,
            timeout=30,
            headers={"X-Client-Type": "cli", "X-ABR-API-Version": "1.0"},
            verify=False,
        )

    @patch("python.cli.api.client.requests.request")
    def test_info_fetches_backup_by_id(self, request_mock: object) -> None:
        request_mock.return_value = _FakeResponse({"id": "backup-1", "status": "available"})
        args = SimpleNamespace(base_url="http://example.test", output="json", log_level="info", id="backup-1")

        stream = io.StringIO()
        with redirect_stdout(stream):
            exit_code = BackupCommands().info(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stream.getvalue())["id"], "backup-1")
        request_mock.assert_called_once_with(
            "GET",
            "http://example.test/api/v1/backups/backup-1",
            json=None,
            params=None,
            timeout=30,
            headers={"X-Client-Type": "cli", "X-ABR-API-Version": "1.0"},
            verify=False,
        )

    @patch("python.cli.api.client.requests.request")
    def test_info_table_output_shows_a_restore_points_table_with_size_and_encryption(
        self, request_mock: object
    ) -> None:
        request_mock.return_value = _FakeResponse(
            {
                "id": "backup-1",
                "status": "available",
                "restore_points": [
                    {
                        "id": "rp-1",
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "execution_type": "ActiveFull",
                        "status": "available",
                        "size_bytes": 1073741824,
                        "compression_level": 6,
                        "encryption_enabled": True,
                    }
                ],
            }
        )
        args = SimpleNamespace(base_url="http://example.test", output="table", log_level="info", id="backup-1")

        stream = io.StringIO()
        with redirect_stdout(stream):
            exit_code = BackupCommands().info(args)

        output = stream.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("backup-1", output)
        self.assertIn("SIZE", output)
        self.assertIn("COMPRESSION", output)
        self.assertIn("ENCRYPTED", output)
        self.assertIn("1.0 GB", output)
        self.assertIn("6", output)
        self.assertIn("True", output)
        self.assertNotIn("1073741824", output)

    @patch("python.cli.api.client.requests.request")
    def test_info_table_output_with_no_restore_points_prints_only_the_summary(
        self, request_mock: object
    ) -> None:
        request_mock.return_value = _FakeResponse({"id": "backup-1", "status": "available", "restore_points": []})
        args = SimpleNamespace(base_url="http://example.test", output="table", log_level="info", id="backup-1")

        stream = io.StringIO()
        with redirect_stdout(stream):
            exit_code = BackupCommands().info(args)

        self.assertEqual(exit_code, 0)
        self.assertNotIn("SIZE", stream.getvalue())

    @patch("python.cli.api.client.requests.request")
    def test_delete_removes_backup_by_id(self, request_mock: object) -> None:
        request_mock.return_value = _FakeResponse(
            {"id": "backup-1", "deleted": True, "restore_point_count": 1, "artifacts_removed": 3}
        )
        args = SimpleNamespace(base_url="http://example.test", output="table", log_level="info", id="backup-1")

        stream = io.StringIO()
        with redirect_stdout(stream):
            exit_code = BackupCommands().delete(args)

        self.assertEqual(exit_code, 0)
        self.assertIn("backup-1", stream.getvalue())
        request_mock.assert_called_once_with(
            "DELETE",
            "http://example.test/api/v1/backups/backup-1",
            json=None,
            params=None,
            timeout=30,
            headers={"X-Client-Type": "cli", "X-ABR-API-Version": "1.0"},
            verify=False,
        )

    @patch("python.cli.api.client.requests.request")
    def test_verify_starts_a_session_and_keeps_the_extended_timeout(self, request_mock: object) -> None:
        request_mock.return_value = _FakeResponse(
            {
                "id": "backup-1",
                "result": "valid",
                "checked_restore_points": 1,
                "checked_artifacts": 3,
                "warnings": [],
                "errors": [],
            }
        )
        args = SimpleNamespace(base_url="http://example.test", output="json", log_level="info", id="backup-1")

        with redirect_stdout(io.StringIO()):
            exit_code = BackupCommands().verify(args)

        self.assertEqual(exit_code, 0)
        request_mock.assert_called_once_with(
            "POST",
            "http://example.test/api/v1/backups/backup-1/verify",
            json=None,
            params={"wait": "false"},
            timeout=21600,
            headers={"X-Client-Type": "cli", "X-ABR-API-Version": "1.0"},
            verify=False,
        )

    def test_verify_follows_the_session_progress_and_prints_its_report(self) -> None:
        report = {
            "id": "backup-1",
            "result": "valid",
            "checked_restore_points": 2,
            "checked_artifacts": 4,
            "warnings": [],
            "errors": [],
        }
        args = SimpleNamespace(base_url="http://example.test", output="json", log_level="info", id="backup-1")

        with patch("python.cli.commands.backup_commands.watch_session") as watch_mock:
            watch_mock.return_value = {"id": "sess-1", "state": "completed", "progress": 100, "verify": report}
            with patch("python.cli.api.client.requests.request") as request_mock:
                request_mock.return_value = _FakeResponse(
                    {"id": "backup-1", "session_id": "sess-1", "state": "running", "result": "running"}
                )
                with redirect_stdout(io.StringIO()) as out:
                    exit_code = BackupCommands().verify(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(watch_mock.call_args.args[1], "sess-1")
        self.assertEqual(json.loads(out.getvalue()), report)

    def test_verify_reports_a_corrupt_chain_found_by_the_session_as_a_non_zero_exit(self) -> None:
        report = {
            "id": "backup-1",
            "result": "invalid",
            "checked_restore_points": 1,
            "checked_artifacts": 1,
            "warnings": [],
            "errors": [{"disk_key": 1, "message": "backup is corrupt", "point_id": "p-1"}],
        }
        args = SimpleNamespace(base_url="http://example.test", output="json", log_level="info", id="backup-1")

        with patch("python.cli.commands.backup_commands.watch_session") as watch_mock:
            watch_mock.return_value = {"id": "sess-1", "state": "failed", "progress": 100, "verify": report}
            with patch("python.cli.api.client.requests.request") as request_mock:
                request_mock.return_value = _FakeResponse({"id": "backup-1", "session_id": "sess-1"})
                with redirect_stdout(io.StringIO()) as out:
                    exit_code = BackupCommands().verify(args)

        self.assertEqual(exit_code, INVALID_STATE)
        self.assertEqual(json.loads(out.getvalue())["result"], "invalid")

    def test_verify_falls_back_to_the_session_when_it_carries_no_report(self) -> None:
        args = SimpleNamespace(base_url="http://example.test", output="json", log_level="info", id="backup-1")

        with patch("python.cli.commands.backup_commands.watch_session") as watch_mock:
            watch_mock.return_value = {"id": "sess-1", "state": "stopped", "progress": 42}
            with patch("python.cli.api.client.requests.request") as request_mock:
                request_mock.return_value = _FakeResponse({"id": "backup-1", "session_id": "sess-1"})
                with redirect_stdout(io.StringIO()) as out:
                    exit_code = BackupCommands().verify(args)

        self.assertEqual(exit_code, INVALID_STATE)
        self.assertEqual(json.loads(out.getvalue())["state"], "stopped")

    @patch("python.cli.api.client.requests.request")
    def test_verify_reports_an_invalid_chain_as_a_non_zero_exit(self, request_mock: object) -> None:
        request_mock.return_value = _FakeResponse(
            {
                "id": "backup-1",
                "result": "invalid",
                "checked_restore_points": 1,
                "checked_artifacts": 1,
                "warnings": [],
                "errors": [{"disk_key": 1, "message": "Backup artifact not found in storage", "point_id": "p-1"}],
            }
        )
        args = SimpleNamespace(base_url="http://example.test", output="json", log_level="info", id="backup-1")

        with redirect_stdout(io.StringIO()) as out:
            exit_code = BackupCommands().verify(args)

        self.assertEqual(json.loads(out.getvalue())["result"], "invalid")
        self.assertEqual(exit_code, INVALID_STATE)

    @patch("python.cli.api.client.requests.request")
    def test_verify_reports_a_warning_result_as_a_non_zero_exit_too(self, request_mock: object) -> None:
        request_mock.return_value = _FakeResponse(
            {
                "id": "backup-1",
                "result": "warning",
                "checked_restore_points": 1,
                "checked_artifacts": 0,
                "warnings": [{"point_id": "p-1", "message": "Incremental restore point does not have a recorded parent"}],
                "errors": [],
            }
        )
        args = SimpleNamespace(base_url="http://example.test", output="json", log_level="info", id="backup-1")

        with redirect_stdout(io.StringIO()):
            exit_code = BackupCommands().verify(args)

        self.assertEqual(exit_code, INVALID_STATE)

    @patch("python.cli.api.client.requests.request")
    def test_import_posts_path_and_repository_selector(self, request_mock: object) -> None:
        request_mock.return_value = _FakeResponse(
            {"id": "backup-2", "name": "imported", "status": "available", "restore_point_count": 1}
        )
        args = SimpleNamespace(
            base_url="http://example.test",
            output="json",
            log_level="info",
            path="/mnt/external/backup",
            repo="Archive",
            repo_id=None,
        )

        with redirect_stdout(io.StringIO()):
            exit_code = BackupCommands().import_backup(args)

        self.assertEqual(exit_code, 0)
        request_mock.assert_called_once_with(
            "POST",
            "http://example.test/api/v1/backups/import",
            json={"path": "/mnt/external/backup", "repo": "Archive", "repo_id": None},
            params=None,
            timeout=30,
            headers={"X-Client-Type": "cli", "X-ABR-API-Version": "1.0"},
            verify=False,
        )


class SessionCommandTests(unittest.TestCase):
    @patch("python.cli.api.client.requests.request")
    def test_list_filters_by_job_and_prints_table(self, request_mock: object) -> None:
        request_mock.return_value = _FakeResponse(
            {
                "items": [
                    {
                        "id": "sess-1",
                        "state": "running",
                        "type": "backup",
                        "job": {"id": "job-1", "name": "Nightly"},
                        "started_at": "2026-05-26T20:30:00+00:00",
                        "finished_at": None,
                        "resolved_backup_type": "ActiveFull",
                        "progress": 40,
                    }
                ]
            }
        )
        args = SimpleNamespace(base_url="http://example.test", output="table", log_level="info", job="Nightly")

        stream = io.StringIO()
        with redirect_stdout(stream):
            exit_code = SessionCommands().list(args)

        self.assertEqual(exit_code, 0)
        self.assertIn("sess-1", stream.getvalue())
        request_mock.assert_called_once_with(
            "GET",
            "http://example.test/api/v1/sessions",
            json=None,
            params={"job": "Nightly"},
            timeout=30,
            headers={"X-Client-Type": "cli", "X-ABR-API-Version": "1.0"},
            verify=False,
        )

    @patch("python.cli.api.client.requests.request")
    def test_list_without_job_sends_no_params(self, request_mock: object) -> None:
        request_mock.return_value = _FakeResponse({"items": []})
        args = SimpleNamespace(base_url="http://example.test", output="json", log_level="info", job=None)

        with redirect_stdout(io.StringIO()):
            SessionCommands().list(args)

        request_mock.assert_called_once_with(
            "GET",
            "http://example.test/api/v1/sessions",
            json=None,
            params=None,
            timeout=30,
            headers={"X-Client-Type": "cli", "X-ABR-API-Version": "1.0"},
            verify=False,
        )

    @patch("python.cli.api.client.requests.request")
    def test_info_fetches_session_by_id(self, request_mock: object) -> None:
        request_mock.return_value = _FakeResponse({"id": "sess-1", "state": "completed"})
        args = SimpleNamespace(base_url="http://example.test", output="json", log_level="info", id="sess-1")

        stream = io.StringIO()
        with redirect_stdout(stream):
            exit_code = SessionCommands().info(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stream.getvalue())["state"], "completed")
        request_mock.assert_called_once_with(
            "GET",
            "http://example.test/api/v1/sessions/sess-1",
            json=None,
            params=None,
            timeout=30,
            headers={"X-Client-Type": "cli", "X-ABR-API-Version": "1.0"},
            verify=False,
        )

    def test_info_with_watch_uses_watch_session_instead_of_a_plain_get(self) -> None:
        args = SimpleNamespace(base_url="http://example.test", output="json", log_level="info", id="sess-1", watch=True)

        with patch("python.cli.commands.session_commands.watch_session") as watch_mock:
            watch_mock.return_value = {"id": "sess-1", "state": "completed"}
            stream = io.StringIO()
            with redirect_stdout(stream):
                exit_code = SessionCommands().info(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stream.getvalue())["state"], "completed")
        self.assertEqual(watch_mock.call_args.args[1], "sess-1")

    def test_info_with_watch_returns_non_zero_when_the_followed_session_failed(self) -> None:
        args = SimpleNamespace(base_url="http://example.test", output="json", log_level="info", id="sess-1", watch=True)

        with patch("python.cli.commands.session_commands.watch_session") as watch_mock:
            watch_mock.return_value = {"id": "sess-1", "state": "failed", "message": "[Errno 13] Permission denied"}
            stream = io.StringIO()
            with redirect_stdout(stream):
                exit_code = SessionCommands().info(args)

        self.assertEqual(json.loads(stream.getvalue())["state"], "failed")
        self.assertEqual(exit_code, INVALID_STATE)

    def test_info_without_watch_stays_zero_even_for_an_already_failed_session(self) -> None:
        args = SimpleNamespace(base_url="http://example.test", output="json", log_level="info", id="sess-1", watch=False)

        with patch("python.cli.api.client.AltaStorApiClient.get") as get_mock:
            get_mock.return_value = {"id": "sess-1", "state": "failed"}
            stream = io.StringIO()
            with redirect_stdout(stream):
                exit_code = SessionCommands().info(args)

        self.assertEqual(exit_code, 0)

    @patch("python.cli.api.client.requests.request")
    def test_log_fetches_session_log(self, request_mock: object) -> None:
        request_mock.return_value = _FakeResponse({"id": "sess-1", "logs": [{"level": "info", "message": "Started"}]})
        args = SimpleNamespace(base_url="http://example.test", output="json", log_level="info", id="sess-1")

        stream = io.StringIO()
        with redirect_stdout(stream):
            exit_code = SessionCommands().log(args)

        self.assertEqual(exit_code, 0)
        self.assertIn("Started", stream.getvalue())
        request_mock.assert_called_once_with(
            "GET",
            "http://example.test/api/v1/sessions/sess-1/log",
            json=None,
            params=None,
            timeout=30,
            headers={"X-Client-Type": "cli", "X-ABR-API-Version": "1.0"},
            verify=False,
        )

    @patch("python.cli.api.client.requests.request")
    def test_stop_posts_force_flag(self, request_mock: object) -> None:
        request_mock.return_value = _FakeResponse({"id": "sess-1", "state": "stopping", "message": "Stop requested"})
        args = SimpleNamespace(base_url="http://example.test", output="json", log_level="info", id="sess-1", force=True)

        with redirect_stdout(io.StringIO()):
            exit_code = SessionCommands().stop(args)

        self.assertEqual(exit_code, 0)
        request_mock.assert_called_once_with(
            "POST",
            "http://example.test/api/v1/sessions/sess-1/stop",
            json={"force": True},
            params=None,
            timeout=30,
            headers={"X-Client-Type": "cli", "X-ABR-API-Version": "1.0"},
            verify=False,
        )

    @patch("python.cli.api.client.requests.request")
    def test_stop_without_force_sends_false(self, request_mock: object) -> None:
        request_mock.return_value = _FakeResponse({"id": "sess-1", "state": "stopping", "message": "Stop requested"})
        args = SimpleNamespace(base_url="http://example.test", output="json", log_level="info", id="sess-1", force=False)

        with redirect_stdout(io.StringIO()):
            SessionCommands().stop(args)

        request_mock.assert_called_once_with(
            "POST",
            "http://example.test/api/v1/sessions/sess-1/stop",
            json={"force": False},
            params=None,
            timeout=30,
            headers={"X-Client-Type": "cli", "X-ABR-API-Version": "1.0"},
            verify=False,
        )


class SystemCommandTests(unittest.TestCase):
    @staticmethod
    def _command() -> SystemCommands:
        return SystemCommands(
            product_name="AltaStor Backup & Replication",
            cli_version="0.1.0",
            default_base_url="http://127.0.0.1:8000",
        )

    @patch("python.cli.api.client.requests.request")
    def test_version_fetches_and_prints_product_info(self, request_mock: object) -> None:
        request_mock.return_value = _FakeResponse(
            {"product": "AltaStor Backup & Replication", "version": "4.1.0", "cli_binary": "abr"}
        )
        args = SimpleNamespace(base_url="http://example.test", output="json", log_level="info")

        stream = io.StringIO()
        with redirect_stdout(stream):
            exit_code = self._command().version(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stream.getvalue())["version"], "4.1.0")
        request_mock.assert_called_once_with(
            "GET",
            "http://example.test/api/v1/system/version",
            json=None,
            params=None,
            timeout=30,
            headers={"X-Client-Type": "cli", "X-ABR-API-Version": "1.0"},
            verify=False,
        )

    @patch("python.cli.api.client.requests.request")
    def test_health_prints_status_and_components(self, request_mock: object) -> None:
        request_mock.return_value = _FakeResponse({"status": "ok", "components": {"api": "ok"}})
        args = SimpleNamespace(base_url="http://example.test", output="table", log_level="info")

        stream = io.StringIO()
        with redirect_stdout(stream):
            exit_code = self._command().health(args)

        self.assertEqual(exit_code, 0)
        self.assertIn("STATUS", stream.getvalue())
        request_mock.assert_called_once_with(
            "GET",
            "http://example.test/api/v1/system/health",
            json=None,
            params=None,
            timeout=30,
            headers={"X-Client-Type": "cli", "X-ABR-API-Version": "1.0"},
            verify=False,
        )

    @patch("python.cli.api.client.requests.request")
    def test_health_prints_a_degraded_catalog_and_exits_non_zero(self, request_mock: object) -> None:
        request_mock.return_value = _FakeResponse(
            {
                "status": "degraded",
                "components": {"api": "ok", "database": "unavailable"},
            }
        )
        args = SimpleNamespace(base_url="http://example.test", output="table", log_level="info")

        stream = io.StringIO()
        with redirect_stdout(stream):
            exit_code = self._command().health(args)

        output = stream.getvalue()
        self.assertIn("degraded", output)
        self.assertIn("unavailable", output)
        self.assertEqual(exit_code, INVALID_STATE)

    @patch("python.cli.api.client.requests.request")
    def test_health_json_output_carries_the_whole_body(self, request_mock: object) -> None:
        payload = {
            "status": "degraded",
            "components": {"api": "ok", "database": "unavailable"},
            "counts": {"repositories": 2},
        }
        request_mock.return_value = _FakeResponse(payload)
        args = SimpleNamespace(base_url="http://example.test", output="json", log_level="info")

        stream = io.StringIO()
        with redirect_stdout(stream):
            exit_code = self._command().health(args)

        self.assertEqual(json.loads(stream.getvalue()), payload)
        self.assertEqual(exit_code, INVALID_STATE)

    @patch("python.cli.api.client.requests.request")
    def test_export_writes_server_provided_archive_using_env_password(self, request_mock: object) -> None:
        manifest = {"counts": {"repositories": 1}, "configuration_keys": ["ALTASTOR_BACKUP_PORT"]}
        archive_bytes = encrypt_payload(
            {"manifest": manifest, "catalog": {"repositories": {}}},
            "TestPassword123",
            iterations=1000,
        )
        request_mock.return_value = _FakeResponse(
            {"manifest": manifest, "archive_base64": base64.b64encode(archive_bytes).decode("ascii")}
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            destination = str(Path(tmpdir) / "export.enc")
            args = SimpleNamespace(base_url="http://example.test", output="table", log_level="info", file=destination)

            with patch.dict(os.environ, {PASSWORD_ENV_VAR: "TestPassword123"}):
                stream = io.StringIO()
                with redirect_stdout(stream):
                    exit_code = self._command().export(args)

            self.assertEqual(exit_code, 0)
            output = stream.getvalue()
            self.assertIn("Wrote encrypted export", output)
            self.assertIn("repositories=1", output)
            self.assertIn("ALTASTOR_BACKUP_PORT", output)

            self.assertEqual(Path(destination).read_bytes(), archive_bytes)

        request_mock.assert_called_once_with(
            "POST",
            "http://example.test/api/v1/system/catalog-snapshot",
            json={"password": "TestPassword123"},
            params=None,
            timeout=30,
            headers={"X-Client-Type": "cli", "X-ABR-API-Version": "1.0"},
            verify=False,
        )

    @patch("python.cli.api.client.requests.request")
    def test_import_sends_archive_and_password_and_posts_catalog_restore(self, request_mock: object) -> None:
        archive_bytes = encrypt_payload(
            {"manifest": {"format_version": 1}, "catalog": {"repositories": {}}},
            "TestPassword123",
            iterations=1000,
        )
        request_mock.return_value = _FakeResponse({"restored": {"repositories": 1}, "configuration": None})

        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = Path(tmpdir) / "export.enc"
            archive_path.write_bytes(archive_bytes)
            args = SimpleNamespace(
                base_url="http://example.test", output="table", log_level="info", file=str(archive_path)
            )

            with patch.dict(os.environ, {PASSWORD_ENV_VAR: "TestPassword123"}):
                stream = io.StringIO()
                with redirect_stdout(stream):
                    exit_code = self._command().import_(args)

        self.assertEqual(exit_code, 0)
        self.assertIn("Import complete", stream.getvalue())
        self.assertIn("repositories=1", stream.getvalue())
        request_mock.assert_called_once_with(
            "POST",
            "http://example.test/api/v1/system/catalog-restore",
            json={
                "password": "TestPassword123",
                "archive_base64": base64.b64encode(archive_bytes).decode("ascii"),
            },
            params=None,
            timeout=30,
            headers={"X-Client-Type": "cli", "X-ABR-API-Version": "1.0"},
            verify=False,
        )

    @patch("python.cli.api.client.requests.request")
    def test_import_writes_recovered_configuration_sidecar_file(self, request_mock: object) -> None:
        archive_bytes = encrypt_payload({"manifest": {"format_version": 1}}, "TestPassword123", iterations=1000)
        request_mock.return_value = _FakeResponse(
            {"restored": {"repositories": 0}, "configuration": {"ALTASTOR_BACKUP_PORT": "9443"}}
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = Path(tmpdir) / "export.enc"
            archive_path.write_bytes(archive_bytes)
            args = SimpleNamespace(
                base_url="http://example.test", output="table", log_level="info", file=str(archive_path)
            )

            with patch.dict(os.environ, {PASSWORD_ENV_VAR: "TestPassword123"}):
                stream = io.StringIO()
                with redirect_stdout(stream):
                    exit_code = self._command().import_(args)

            self.assertEqual(exit_code, 0)
            output = stream.getvalue()
            recovered_path = Path(f"{archive_path}-default.env")
            self.assertTrue(recovered_path.exists())
            self.assertIn("ALTASTOR_BACKUP_PORT=9443", recovered_path.read_text(encoding="utf-8"))
            self.assertIn(f"Recovered server configuration written to {recovered_path}. ", output)
            self.assertIn("This is NOT applied automatically:", output)

    def test_import_propagates_wrong_password_error_from_server(self) -> None:
        import requests

        with tempfile.TemporaryDirectory() as tmpdir:
            archive_path = Path(tmpdir) / "export.enc"
            archive_path.write_bytes(b"irrelevant-bytes-the-server-does-the-decrypting")
            args = SimpleNamespace(
                base_url="http://example.test", output="table", log_level="info", file=str(archive_path)
            )

            response = SimpleNamespace(
                status_code=422,
                json=lambda: {
                    "error": {
                        "code": "wrong_password",
                        "message": "Incorrect password, or the archive is corrupted/has been tampered with",
                    }
                },
                text="",
            )
            error = requests.RequestException("422 Client Error")
            error.response = response

            with patch.dict(os.environ, {PASSWORD_ENV_VAR: "WrongPassword"}), patch(
                "python.cli.api.client.requests.request", side_effect=error
            ) as request_mock:
                with self.assertRaises(requests.RequestException):
                    self._command().import_(args)

        request_mock.assert_called_once()

    def test_import_missing_file_fails_cleanly(self) -> None:
        args = SimpleNamespace(
            base_url="http://example.test",
            output="table",
            log_level="info",
            file="/nonexistent/path/export.enc",
        )

        with patch.dict(os.environ, {PASSWORD_ENV_VAR: "AnyPassword"}), patch(
            "python.cli.api.client.requests.request"
        ) as request_mock:
            stream_err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(stream_err):
                exit_code = self._command().import_(args)

        self.assertEqual(exit_code, 1)
        self.assertIn("No such file", stream_err.getvalue())
        request_mock.assert_not_called()

    def test_export_without_env_password_and_no_tty_raises_clean_usage_error(self) -> None:
        args = SimpleNamespace(base_url="http://example.test", output="table", log_level="info", file="/tmp/out.enc")

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(PASSWORD_ENV_VAR, None)
            with patch("python.cli.commands.system_commands.getpass.getpass", side_effect=EOFError):
                with self.assertRaises(CliStructuredError) as ctx:
                    self._command().export(args)

        self.assertEqual(ctx.exception.exit_code, INVALID_USAGE)
        self.assertIn(PASSWORD_ENV_VAR, ctx.exception.message)

    def test_import_without_env_password_and_no_tty_raises_clean_usage_error(self) -> None:
        args = SimpleNamespace(base_url="http://example.test", output="table", log_level="info", file="/tmp/out.enc")

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(PASSWORD_ENV_VAR, None)
            with patch("python.cli.commands.system_commands.getpass.getpass", side_effect=EOFError):
                with self.assertRaises(CliStructuredError) as ctx:
                    self._command().import_(args)

        self.assertEqual(ctx.exception.exit_code, INVALID_USAGE)
        self.assertIn(PASSWORD_ENV_VAR, ctx.exception.message)

    @patch("python.cli.api.client.requests.request")
    def test_get_support_bundle_writes_archive_to_explicit_file(self, request_mock: object) -> None:
        archive_bytes = b"PK\x03\x04fake-zip-bytes"
        manifest = {"files": ["manifest.json", "system-info.json"], "warnings": []}
        request_mock.return_value = _FakeResponse(
            {"manifest": manifest, "archive_base64": base64.b64encode(archive_bytes).decode("ascii")}
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            destination = str(Path(tmpdir) / "bundle.zip")
            args = SimpleNamespace(base_url="http://example.test", output="table", log_level="info", file=destination)

            stream = io.StringIO()
            with redirect_stdout(stream):
                exit_code = self._command().get_support_bundle(args)

            self.assertEqual(exit_code, 0)
            self.assertIn(f"Wrote support bundle to {destination}", stream.getvalue())
            self.assertIn("2 files", stream.getvalue())
            self.assertNotIn(
                "system-info.json",
                stream.getvalue(),
                "individual names belong in manifest.json, not on the terminal",
            )
            self.assertEqual(Path(destination).read_bytes(), archive_bytes)

        request_mock.assert_called_once_with(
            "POST",
            "http://example.test/api/v1/system/support-bundle",
            json=None,
            params=None,
            timeout=30,
            headers={"X-Client-Type": "cli", "X-ABR-API-Version": "1.0"},
            verify=False,
        )

    @patch("python.cli.api.client.requests.request")
    def test_get_support_bundle_uses_timestamped_default_filename_when_file_omitted(self, request_mock: object) -> None:
        archive_bytes = b"PK\x03\x04fake-zip-bytes"
        request_mock.return_value = _FakeResponse(
            {"manifest": {"files": [], "warnings": []}, "archive_base64": base64.b64encode(archive_bytes).decode("ascii")}
        )
        args = SimpleNamespace(base_url="http://example.test", output="table", log_level="info", file=None)

        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            os.chdir(tmpdir)
            try:
                stream = io.StringIO()
                with redirect_stdout(stream):
                    exit_code = self._command().get_support_bundle(args)

                self.assertEqual(exit_code, 0)
                written = list(Path(tmpdir).glob("support-bundle-*.zip"))
                self.assertEqual(len(written), 1)
                self.assertEqual(written[0].read_bytes(), archive_bytes)
                self.assertIn(written[0].name, stream.getvalue())

                printed_line = next(
                    line for line in stream.getvalue().splitlines() if line.startswith("Wrote support bundle to ")
                )
                reported_path = printed_line.removeprefix("Wrote support bundle to ").rsplit(" (", 1)[0]
                self.assertTrue(Path(reported_path).is_absolute())
                self.assertEqual(Path(reported_path), written[0].resolve())
            finally:
                os.chdir(cwd)

    @patch("python.cli.api.client.requests.request")
    def test_get_support_bundle_prints_manifest_warnings_to_stderr(self, request_mock: object) -> None:
        request_mock.return_value = _FakeResponse(
            {
                "manifest": {"files": [], "warnings": ["MongoDB log not found at /var/log/altastor-backup/mongodb/mongod.log"]},
                "archive_base64": base64.b64encode(b"PK\x03\x04").decode("ascii"),
            }
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            args = SimpleNamespace(
                base_url="http://example.test",
                output="table",
                log_level="info",
                file=str(Path(tmpdir) / "bundle.zip"),
            )
            stream_err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(stream_err):
                exit_code = self._command().get_support_bundle(args)

        self.assertEqual(exit_code, 0)
        self.assertIn("Warning: MongoDB log not found", stream_err.getvalue())


if __name__ == "__main__":
    unittest.main()
