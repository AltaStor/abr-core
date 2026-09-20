# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError:
    for site_packages in sorted((pathlib.Path(__file__).resolve().parents[2] / ".venv" / "lib").glob("python*/site-packages")):
        sys.path.insert(0, str(site_packages))
from fastapi.testclient import TestClient

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
    apply_repository_immutability,
    disk_tool_immutability_args,
    immutability_is_enabled,
    normalize_immutability_config,
    parse_disk_tool_json,
    set_disk_tool_backup_retention,
)
from python.srv.core.subprocess_util import CompletedProcess
from python.cli.commands import repository_commands
from python.srv.plugins.vcenter import vcenter_backup
from python.srv.services import abr_v41
from python.srv.services.abr_v41 import DAY_ORDER
from python.srv.services.backup_mode import object_lock_actual_retention_days
from python.srv.services.api_errors import ApiError

WORM_REFUSAL = "deleteObject: InvalidRequest: Object is WORM protected and cannot be overwritten"

S3_SETTINGS = {
    "bucket": "altastor-objectlock",
    "prefix": "lab",
    "region": "us-east-1",
    "endpoint_url": "http://minio.local:9000",
    "access_key": "AKIAEXAMPLE",
    "secret_key": "secret",
    "path_style": True,
}


def _json_result(payload: dict[str, object], *, progress: bool = False) -> str:
    prefix = "50%\n100%\n" if progress else ""
    return prefix + json.dumps(payload)


def _disk_tool_stub(
    *,
    supported_mode: str = "compliance",
    default_mode: str = "none",
    default_days: int = 0,
    stored_mode: str | None = None,
    worm_enforced: bool = True,
    immutable_backup_error: str | None = None,
    retention_error: str | None = None,
    leftover_probes: tuple[str, ...] = (),
):
    calls: list[list[str]] = []
    locked: dict[str, tuple[str, int]] = {}

    def _named(command: list[str]) -> str:
        return command[command.index("--name") + 1] if "--name" in command else ""

    def _side_effect(command: list[str], **_kwargs: object) -> CompletedProcess:
        calls.append(list(command))
        head = command[0] if command else ""
        if head == "info":
            if "--name" in command:
                mode, days = locked.get(_named(command), (default_mode, default_days))
                body = {
                    "command": "info",
                    "status": "ok",
                    "immutability_supported": supported_mode,
                    "immutability": stored_mode if stored_mode is not None else mode,
                    "retention_days": days,
                }
            else:
                body = {
                    "command": "info",
                    "status": "ok",
                    "immutability_supported": supported_mode,
                    "immutability": default_mode,
                    "retention_days": default_days,
                }
            return CompletedProcess(args=command, returncode=0, stdout=_json_result(body), stderr="")
        if head == "backup":
            if "--immutable" in command:
                if immutable_backup_error:
                    raise StorageToolError(immutable_backup_error)
                locked[_named(command)] = (
                    command[command.index("--immutable") + 1],
                    int(command[command.index("--retention") + 1]),
                )
            return CompletedProcess(
                args=command,
                returncode=0,
                stdout=_json_result({"command": "backup", "status": "ok"}, progress=True),
                stderr="",
            )
        if head == "list":
            listing = "Backups in 'probe':\n" + "".join(f"  {name}\n" for name in leftover_probes)
            return CompletedProcess(args=command, returncode=0, stdout=listing, stderr="")
        if head == "retention":
            if retention_error:
                raise StorageToolError(retention_error)
            locked[_named(command)] = (
                command[command.index("--immutable") + 1],
                int(command[command.index("--retention") + 1]),
            )
            return CompletedProcess(
                args=command,
                returncode=0,
                stdout=_json_result({"command": "retention", "status": "ok"}),
                stderr="",
            )
        if head == "remove":
            name = _named(command)
            if worm_enforced and name in locked:
                raise StorageToolError(WORM_REFUSAL)
            locked.pop(name, None)
            return CompletedProcess(args=command, returncode=0, stdout="", stderr="")
        return CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    _side_effect.calls = calls
    return _side_effect


def _checks_by_name(result: dict[str, object]) -> dict[str, dict[str, object]]:
    return {str(check["name"]): check for check in result.get("checks") or []}


class S3ImmutabilityConfigTests(unittest.TestCase):
    def test_normalize_immutability_config_defaults_to_disabled(self) -> None:
        self.assertEqual(
            normalize_immutability_config(None),
            {"mode": "none", "retention_days": 0, "retention_scope": "job_retention"},
        )
        self.assertEqual(
            normalize_immutability_config({"mode": "GOVERNANCE", "retention_days": "30"}),
            {"mode": "governance", "retention_days": 30, "retention_scope": "job_retention"},
        )
        self.assertEqual(
            normalize_immutability_config({"mode": "governance", "retention_days": "junk"}),
            {"mode": "governance", "retention_days": 0, "retention_scope": "job_retention"},
        )
        self.assertEqual(
            normalize_immutability_config({"mode": "governance", "retention_days": 30, "retention_scope": "minimum-only"})["retention_scope"],
            "minimum_only",
        )

    def test_immutability_is_enabled_needs_both_halves(self) -> None:
        self.assertFalse(immutability_is_enabled({"mode": "governance", "retention_days": 0}))
        self.assertFalse(immutability_is_enabled({"mode": "none", "retention_days": 30}))
        self.assertTrue(immutability_is_enabled({"mode": "compliance", "retention_days": 1}))

    def test_disk_tool_immutability_args_only_for_enabled_s3_repositories(self) -> None:
        enabled = {"type": "s3", "s3": {"bucket": "b", "immutability": {"mode": "governance", "retention_days": 30}}}
        self.assertEqual(disk_tool_immutability_args(enabled), ["--immutable", "governance", "--retention", "30"])
        self.assertEqual(disk_tool_immutability_args({"type": "s3", "s3": {"bucket": "b"}}), [])
        self.assertEqual(
            disk_tool_immutability_args({"type": "local", "immutability": {"mode": "governance", "retention_days": 30}}),
            [],
        )

    def test_parse_disk_tool_json_skips_progress_lines(self) -> None:
        document = parse_disk_tool_json('2%\n100%\n{"command": "backup", "status": "ok"}\n')
        self.assertEqual(document["status"], "ok")
        with self.assertRaises(StorageToolError):
            parse_disk_tool_json("100%\n")


class S3ImmutabilityRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS):
            store.clear()

    def tearDown(self) -> None:
        for store in (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS):
            store.clear()

    def _create_repository(self, immutability: dict[str, object] | None = None, **overrides: object) -> dict[str, object]:
        settings = dict(S3_SETTINGS)
        settings.update(overrides)
        if immutability is not None:
            settings["immutability"] = immutability
        return abr_v41.create_repository_record({"name": "ObjectRepo", "type": "s3", "s3": settings})

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_repository_without_immutability_stays_disabled(self, run_mock, _connect) -> None:
        run_mock.side_effect = _disk_tool_stub()
        repository = self._create_repository()
        self.assertEqual(repository["immutability"], {"mode": "none", "retention_days": 0, "retention_scope": "job_retention"})
        self.assertTrue(repository["capabilities"]["supports_immutability"])

    def test_local_repository_reports_no_immutability(self) -> None:
        path = Path(self._local_path())
        repository = abr_v41.create_repository_record({"name": "Local", "type": "local", "path": str(path)})
        self.assertIsNone(repository["immutability"])
        self.assertFalse(repository["capabilities"]["supports_immutability"])

    def _local_path(self) -> str:
        import tempfile

        directory = tempfile.mkdtemp(prefix="abr-immutability-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        return directory

    def test_mode_without_retention_period_is_rejected(self) -> None:
        with self.assertRaises(ApiError) as error:
            self._create_repository({"mode": "governance"})
        self.assertEqual(error.exception.status_code, 422)
        self.assertIn("at least 1 day", error.exception.message)

    def test_retention_period_without_mode_is_rejected(self) -> None:
        with self.assertRaises(ApiError) as error:
            self._create_repository({"mode": "none", "retention_days": 30})
        self.assertEqual(error.exception.status_code, 422)
        self.assertIn("requires an immutability mode", error.exception.message)

    def test_unknown_mode_is_rejected(self) -> None:
        with self.assertRaises(ApiError) as error:
            self._create_repository({"mode": "worm", "retention_days": 30})
        self.assertIn("must be one of", error.exception.message)

    def test_retention_period_over_the_cap_is_rejected(self) -> None:
        with self.assertRaises(ApiError) as error:
            self._create_repository({"mode": "governance", "retention_days": 1000})
        self.assertIn("must not exceed", error.exception.message)

    def test_unknown_retention_scope_is_rejected(self) -> None:
        with self.assertRaises(ApiError) as error:
            self._create_repository(
                {"mode": "governance", "retention_days": 30, "retention_scope": "whenever"}
            )
        self.assertIn("retention scope must be one of", error.exception.message)

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_creating_an_immutable_repository_validates_the_bucket(self, run_mock, _connect) -> None:
        run_mock.side_effect = _disk_tool_stub()

        repository = self._create_repository({"mode": "governance", "retention_days": 30})

        self.assertEqual(repository["immutability"], {"mode": "governance", "retention_days": 30, "retention_scope": "job_retention"})
        stored = abr_v41.resolve_repository(record_id=repository["id"])
        self.assertEqual(stored["immutability"], {"mode": "governance", "retention_days": 30, "retention_scope": "job_retention"})
        self.assertEqual(
            disk_tool_immutability_args(stored),
            ["--immutable", "governance", "--retention", "30"],
        )

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_bucket_without_object_lock_cannot_be_configured_for_immutability(self, run_mock, _connect) -> None:
        run_mock.side_effect = _disk_tool_stub(supported_mode="none")

        with self.assertRaises(ApiError) as error:
            self._create_repository({"mode": "governance", "retention_days": 30})

        self.assertEqual(error.exception.status_code, 422)
        self.assertEqual(error.exception.code, "repository_immutability_unsupported")
        self.assertIn("does not support Object Lock", error.exception.message)
        object_lock = _checks_by_name({"checks": error.exception.details.get("checks")})["object_lock"]
        self.assertEqual(object_lock["status"], "failed")
        self.assertIn("only be turned on when a bucket is created", str(object_lock["message"]))
        self.assertEqual(REPOSITORIES, {})

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_compliance_is_rejected_when_the_bucket_only_offers_governance(self, run_mock, _connect) -> None:
        run_mock.side_effect = _disk_tool_stub(supported_mode="governance")

        with self.assertRaises(ApiError) as error:
            self._create_repository({"mode": "compliance", "retention_days": 30})

        self.assertIn("does not support compliance immutability", error.exception.message)

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_bucket_default_rule_stronger_than_the_request_is_rejected(self, run_mock, _connect) -> None:
        run_mock.side_effect = _disk_tool_stub(default_mode="compliance", default_days=10)

        with self.assertRaises(ApiError) as error:
            self._create_repository({"mode": "governance", "retention_days": 30})

        self.assertIn("applies compliance immutability", error.exception.message)

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_bucket_default_period_longer_than_the_request_is_rejected(self, run_mock, _connect) -> None:
        run_mock.side_effect = _disk_tool_stub(default_mode="governance", default_days=90)

        with self.assertRaises(ApiError) as error:
            self._create_repository({"mode": "governance", "retention_days": 30})

        self.assertIn("longer than the requested 30 day(s)", error.exception.message)

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_storage_that_does_not_enforce_the_lock_fails_validation(self, run_mock, _connect) -> None:
        run_mock.side_effect = _disk_tool_stub(worm_enforced=False)


        with self.assertRaises(ApiError) as error:
            self._create_repository({"mode": "governance", "retention_days": 30})

        self.assertIn("did not enforce Object Lock", error.exception.message)

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_rejected_immutable_write_is_reported_with_the_storage_message(self, run_mock, _connect) -> None:
        run_mock.side_effect = _disk_tool_stub(immutable_backup_error="AccessDenied: s3:PutObjectRetention")

        with self.assertRaises(ApiError) as error:
            self._create_repository({"mode": "governance", "retention_days": 30})

        self.assertIn("s3:PutObjectRetention", error.exception.message)


class S3ImmutabilityValidationFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS):
            store.clear()

    def tearDown(self) -> None:
        for store in (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS):
            store.clear()

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_pre_create_test_reports_every_immutability_check(self, run_mock, _connect) -> None:
        run_mock.side_effect = _disk_tool_stub()

        result = abr_v41.test_s3_immutability(
            {"s3": dict(S3_SETTINGS, immutability={"mode": "governance", "retention_days": 30})}
        )

        self.assertEqual(result["status"], "success")
        checks = _checks_by_name(result)
        for name in ("object_lock", "versioning", "immutability_mode", "retention_period",
                     "write_retention", "retention_readback", "worm_enforcement"):
            self.assertEqual(checks[name]["status"], "ok", name)
        self.assertIn(WORM_REFUSAL, str(checks["worm_enforcement"]["message"]))
        self.assertIn("Confirmed by behaviour", str(checks["versioning"]["message"]))

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_probe_is_written_under_a_dedicated_prefix_and_minimum_lock(self, run_mock, _connect) -> None:
        stub = _disk_tool_stub()
        run_mock.side_effect = stub

        abr_v41.test_s3_immutability(
            {"s3": dict(S3_SETTINGS, immutability={"mode": "governance", "retention_days": 30})}
        )

        backup = next(command for command in stub.calls if command[0] == "backup")
        self.assertEqual(backup[backup.index("--path") + 1], "lab/.abr-immutability-probe")
        self.assertEqual(backup[backup.index("--immutable") + 1], "governance")
        self.assertEqual(backup[backup.index("--retention") + 1], "1")
        self.assertNotIn("--create-folder", backup)
        self.assertTrue(backup[backup.index("--name") + 1].startswith("immutability-probe-"))

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_probe_lock_matches_a_bucket_default_rule(self, run_mock, _connect) -> None:
        stub = _disk_tool_stub(default_mode="governance", default_days=7)
        run_mock.side_effect = stub

        abr_v41.test_s3_immutability(
            {"s3": dict(S3_SETTINGS, immutability={"mode": "governance", "retention_days": 30})}
        )

        backup = next(command for command in stub.calls if command[0] == "backup")
        self.assertEqual(backup[backup.index("--retention") + 1], "7")

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_validation_sweeps_probes_an_earlier_run_left_behind(self, run_mock, _connect) -> None:
        stub = _disk_tool_stub(leftover_probes=("immutability-probe-old", "real-backup"))
        run_mock.side_effect = stub

        abr_v41.test_s3_immutability(
            {"s3": dict(S3_SETTINGS, immutability={"mode": "governance", "retention_days": 30})}
        )

        removed = [command[command.index("--name") + 1] for command in stub.calls if command[0] == "remove"]
        self.assertIn("immutability-probe-old", removed)
        self.assertNotIn("real-backup", removed)

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_disabled_immutability_still_reports_bucket_capability(self, run_mock, _connect) -> None:
        stub = _disk_tool_stub()
        run_mock.side_effect = stub

        result = abr_v41.test_s3_immutability({"s3": dict(S3_SETTINGS)})

        self.assertEqual(result["status"], "success")
        self.assertIn("currently disabled", result["message"])
        self.assertEqual(_checks_by_name(result)["immutability_mode"]["status"], "skipped")
        self.assertEqual([command for command in stub.calls if command[0] == "backup"], [])

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_verify_immutability_on_a_stored_repository(self, run_mock, _connect) -> None:
        run_mock.side_effect = _disk_tool_stub()
        repository = abr_v41.create_repository_record(
            {
                "name": "ObjectRepo",
                "type": "s3",
                "s3": dict(S3_SETTINGS, immutability={"mode": "governance", "retention_days": 30}),
            }
        )

        result = abr_v41.verify_repository_immutability(repository["id"])

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["immutability"], {"mode": "governance", "retention_days": 30, "retention_scope": "job_retention"})
        self.assertEqual(result["immutability_effect"]["existing_restore_points"], 0)

    def test_verify_immutability_rejects_non_s3_repositories(self) -> None:
        import shutil
        import tempfile

        directory = tempfile.mkdtemp(prefix="abr-immutability-test-")
        self.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))
        repository = abr_v41.create_repository_record({"name": "Local", "type": "local", "path": directory})

        with self.assertRaises(ApiError) as error:
            abr_v41.verify_repository_immutability(repository["id"])

        self.assertEqual(error.exception.status_code, 422)
        self.assertIn("only available for s3 repositories", error.exception.message)

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_plain_repo_verify_reports_immutability_without_writing_a_locked_probe(self, run_mock, _connect) -> None:
        stub = _disk_tool_stub()
        run_mock.side_effect = stub
        repository = abr_v41.create_repository_record(
            {
                "name": "ObjectRepo",
                "type": "s3",
                "s3": dict(S3_SETTINGS, immutability={"mode": "governance", "retention_days": 30}),
            }
        )
        stub.calls.clear()

        result = abr_v41.verify_repository(repository["id"])

        self.assertEqual(result["status"], "success")
        self.assertIn("governance for 30 day(s)", result["message"])
        checks = _checks_by_name(result)
        self.assertEqual(checks["object_lock"]["status"], "ok")
        self.assertEqual(checks["versioning"]["status"], "skipped")
        self.assertIn("implied here rather than checked", str(checks["versioning"]["message"]))
        self.assertEqual([command for command in stub.calls if "--immutable" in command], [])

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_plain_repo_verify_skips_immutability_when_disabled(self, run_mock, _connect) -> None:
        run_mock.side_effect = _disk_tool_stub(supported_mode="none")
        repository = abr_v41.create_repository_record({"name": "ObjectRepo", "type": "s3", "s3": dict(S3_SETTINGS)})

        result = abr_v41.verify_repository(repository["id"])

        self.assertEqual(result["status"], "success")
        self.assertEqual(_checks_by_name(result)["immutability"]["status"], "skipped")


class S3ImmutabilityChangeReportingTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS):
            store.clear()

    def tearDown(self) -> None:
        for store in (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS):
            store.clear()

    def _repository_with_backups(self, restore_points: int) -> dict[str, object]:
        repository = abr_v41.create_repository_record({"name": "ObjectRepo", "type": "s3", "s3": dict(S3_SETTINGS)})
        for index in range(restore_points):
            BACKUPS[f"point-{index}"] = {
                "id": f"point-{index}",
                "job_id": "job-1",
                "vm_id": "vm-1",
                "repository_id": repository["id"],
                "status": "success",
                "backup_type": "full",
                "created_at": "2026-01-01T00:00:00Z",
            }
        return repository

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_enabling_immutability_reports_that_existing_backups_stay_unprotected(self, run_mock, _connect) -> None:
        run_mock.side_effect = _disk_tool_stub()
        repository = self._repository_with_backups(3)

        result = abr_v41.edit_repository_record(
            repository["id"], {"s3": {"immutability": {"mode": "governance", "retention_days": 30}}}
        )

        effect = result["immutability_effect"]
        self.assertTrue(effect["changed"])
        self.assertFalse(effect["existing_data_protected"])
        self.assertEqual(effect["existing_restore_points"], 3)
        self.assertEqual(effect["previous"], {"mode": "none", "retention_days": 0, "retention_scope": "job_retention"})
        self.assertEqual(effect["current"], {"mode": "governance", "retention_days": 30, "retention_scope": "job_retention"})
        self.assertTrue(any("30 day(s)" in line for line in effect["effect"]))
        self.assertTrue(any("3 restore point(s)" in line for line in effect["limitations"]))
        self.assertTrue(any("repo apply-immutability" in line for line in effect["limitations"]))
        self.assertTrue(any("stay deletable" in line for line in effect["limitations"]))

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_increasing_the_period_reports_the_effect_on_existing_and_new_backups(self, run_mock, _connect) -> None:
        run_mock.side_effect = _disk_tool_stub()
        repository = self._repository_with_backups(2)
        abr_v41.edit_repository_record(
            repository["id"], {"s3": {"immutability": {"mode": "governance", "retention_days": 30}}}
        )

        result = abr_v41.edit_repository_record(repository["id"], {"s3": {"immutability": {"retention_days": 60}}})

        effect = result["immutability_effect"]
        self.assertEqual(effect["previous"], {"mode": "governance", "retention_days": 30, "retention_scope": "job_retention"})
        self.assertEqual(effect["current"], {"mode": "governance", "retention_days": 60, "retention_scope": "job_retention"})
        self.assertTrue(any("60 day(s)" in line for line in effect["effect"]))
        self.assertTrue(any("run 'repo apply-immutability'" in line for line in effect["limitations"]))
        self.assertTrue(
            any("recorded on the restore point" in line for line in effect["limitations"])
        )
        self.assertFalse(
            any("30 day(s)" in line for line in effect["limitations"]),
            "the stored lock is longer than the configured period, so no bare period may be quoted for it",
        )

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_relaxing_immutability_reports_that_stored_locks_cannot_be_shortened(self, run_mock, _connect) -> None:
        run_mock.side_effect = _disk_tool_stub()
        repository = self._repository_with_backups(1)
        abr_v41.edit_repository_record(
            repository["id"], {"s3": {"immutability": {"mode": "governance", "retention_days": 30}}}
        )

        result = abr_v41.edit_repository_record(repository["id"], {"s3": {"immutability": {"mode": "none"}}})

        effect = result["immutability_effect"]
        self.assertEqual(effect["current"], {"mode": "none", "retention_days": 0, "retention_scope": "job_retention"})
        self.assertTrue(any("never be shortened or removed" in line for line in effect["limitations"]))
        stored = abr_v41.resolve_repository(record_id=repository["id"])
        self.assertEqual(disk_tool_immutability_args(stored), [])

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_disabling_immutability_needs_no_bucket_validation(self, run_mock, _connect) -> None:
        run_mock.side_effect = _disk_tool_stub()
        repository = abr_v41.create_repository_record(
            {
                "name": "ObjectRepo",
                "type": "s3",
                "s3": dict(S3_SETTINGS, immutability={"mode": "governance", "retention_days": 30}),
            }
        )
        run_mock.side_effect = _disk_tool_stub(supported_mode="none")

        result = abr_v41.edit_repository_record(repository["id"], {"s3": {"immutability": {"mode": "none"}}})

        self.assertEqual(result["immutability"], {"mode": "none", "retention_days": 0, "retention_scope": "job_retention"})

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_empty_edit_of_a_record_written_before_immutability_is_still_rejected(
        self, run_mock, _connect
    ) -> None:
        run_mock.side_effect = _disk_tool_stub()
        REPOSITORIES["legacy"] = {
            "id": "legacy",
            "name": "Legacy",
            "type": "s3",
            "path": "s3://altastor-objectlock/lab",
            "status": "available",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "metadata": {
                "description": None,
                "last_scan_at": None,
                "s3": {
                    "bucket": "altastor-objectlock",
                    "prefix": "lab",
                    "region": "us-east-1",
                    "endpoint_url": None,
                    "access_key": None,
                    "secret_key": None,
                    "profile": None,
                    "path_style": False,
                    "verify_tls": True,
                    "description": None,
                },
            },
        }

        with self.assertRaises(ApiError) as error:
            abr_v41.edit_repository_record("legacy", {})

        self.assertIn("at least one editable option", error.exception.message)
        self.assertEqual(abr_v41.edit_repository_record("legacy", {"s3": {"prefix": "lab2"}})["prefix"], "lab2")

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_shorter_job_retention_than_the_lock_is_warned_about(self, run_mock, _connect) -> None:
        run_mock.side_effect = _disk_tool_stub()
        repository = abr_v41.create_repository_record({"name": "ObjectRepo", "type": "s3", "s3": dict(S3_SETTINGS)})
        JOBS["job-1"] = {
            "id": "job-1",
            "name": "Nightly",
            "job_type": "backup",
            "repo_id": repository["id"],
            "retention_days": 7,
        }

        result = abr_v41.edit_repository_record(
            repository["id"], {"s3": {"immutability": {"mode": "governance", "retention_days": 30}}}
        )

        warnings = result["immutability_effect"]["warnings"]
        self.assertTrue(any("Nightly" in warning for warning in warnings), warnings)
        self.assertTrue(any("the storage is paid for the whole lock" in warning for warning in warnings))
        self.assertTrue(any("stay locked for 30 day(s)" in warning for warning in warnings), warnings)

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_minimum_only_scope_warns_about_the_unprotected_window(self, run_mock, _connect) -> None:
        run_mock.side_effect = _disk_tool_stub()
        repository = abr_v41.create_repository_record({"name": "ObjectRepo", "type": "s3", "s3": dict(S3_SETTINGS)})
        JOBS["job-1"] = {
            "id": "job-1",
            "name": "Nightly",
            "job_type": "backup",
            "repo_id": repository["id"],
            "retention_days": 90,
        }

        result = abr_v41.edit_repository_record(
            repository["id"],
            {"s3": {"immutability": {"mode": "governance", "retention_days": 30, "retention_scope": "minimum_only"}}},
        )

        warnings = result["immutability_effect"]["warnings"]
        self.assertTrue(any("locked for only 30 day(s)" in warning for warning in warnings), warnings)
        self.assertTrue(any("from day 30 to day 90" in warning for warning in warnings), warnings)


class S3ImmutabilityDeletionReportingTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS):
            store.clear()

    def tearDown(self) -> None:
        for store in (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS):
            store.clear()

    def _repository_with_locked_point(self, immutability: dict[str, object] | None) -> str:
        settings = dict(S3_SETTINGS)
        if immutability is not None:
            settings["immutability"] = immutability
        repository = abr_v41.create_repository_record({"name": "ObjectRepo", "type": "s3", "s3": settings})
        BACKUPS["point-1"] = {
            "id": "point-1",
            "job_id": "job-1",
            "vm_id": "vm-1",
            "repository_id": repository["id"],
            "status": "success",
            "backup_type": "full",
            "execution_type": "Full",
            "created_at": "2026-01-01T00:00:00Z",
        }
        return str(abr_v41.normalize_backup_point(BACKUPS["point-1"])["chain_id"])

    @patch("python.srv.services.abr_v41._delete_backup_artifacts_from_point", return_value=False)
    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_delete_blames_object_lock_when_the_repository_is_immutable(self, run_mock, _connect, _delete) -> None:
        run_mock.side_effect = _disk_tool_stub()
        chain_id = self._repository_with_locked_point({"mode": "governance", "retention_days": 30})

        result = abr_v41.delete_backup_chain(chain_id)

        self.assertFalse(result["artifacts_removed"])
        self.assertFalse(result["deleted"])
        self.assertEqual(result["immutable_restore_points"], 1)
        self.assertEqual(result["retained_restore_point_ids"], ["point-1"])
        self.assertIn("Object Lock", result["message"])
        self.assertIn("kept in the catalog", result["message"])
        self.assertNotIn("storage root", result["message"])
        self.assertIn("point-1", BACKUPS)

    @patch("python.srv.services.abr_v41._delete_backup_artifacts_from_point", return_value=False)
    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_delete_keeps_the_storage_root_explanation_without_immutability(self, run_mock, _connect, _delete) -> None:
        run_mock.side_effect = _disk_tool_stub()
        chain_id = self._repository_with_locked_point(None)

        result = abr_v41.delete_backup_chain(chain_id)

        self.assertFalse(result["artifacts_removed"])
        self.assertTrue(result["deleted"])
        self.assertNotIn("immutable_restore_points", result)
        self.assertIn("storage root", result["message"])
        self.assertNotIn("point-1", BACKUPS)


class S3RetroactiveImmutabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS):
            store.clear()

    def tearDown(self) -> None:
        for store in (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS):
            store.clear()

    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_metadata_only_backup_counts_as_locked(self, run_mock) -> None:
        run_mock.side_effect = StorageToolError(
            "putObjectRetention: 'lab/manifest.abrd': NoSuchKey: The specified key does not exist."
        )
        repository = {"type": "s3", "s3": dict(S3_SETTINGS, immutability={"mode": "governance", "retention_days": 5})}

        result = apply_repository_immutability(repository, "lab", "manifest")

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["metadata_only"])

    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_other_storage_failures_still_propagate(self, run_mock) -> None:
        run_mock.side_effect = StorageToolError("InvalidRequest: Object is WORM protected")
        repository = {"type": "s3", "s3": dict(S3_SETTINGS, immutability={"mode": "governance", "retention_days": 5})}

        with self.assertRaises(StorageToolError):
            apply_repository_immutability(repository, "lab", "manifest")

    def test_retention_command_refuses_to_release_a_held_backup(self) -> None:
        repository = {"type": "s3", "s3": dict(S3_SETTINGS)}
        with self.assertRaises(StorageToolError) as error:
            set_disk_tool_backup_retention(repository, "p", "b", mode="none", retention_days=5)
        self.assertIn("cannot release", str(error.exception))

    def test_retention_command_requires_at_least_one_day(self) -> None:
        repository = {"type": "s3", "s3": dict(S3_SETTINGS)}
        with self.assertRaises(StorageToolError):
            set_disk_tool_backup_retention(repository, "p", "b", mode="governance", retention_days=0)

    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_applying_repository_immutability_is_a_no_op_when_disabled(self, run_mock) -> None:
        run_mock.side_effect = _disk_tool_stub()

        self.assertIsNone(apply_repository_immutability({"type": "s3", "s3": dict(S3_SETTINGS)}, "p", "b"))
        self.assertEqual(run_mock.call_args_list, [])

    def _repository_with_points(self, run_mock, immutability, point_count=2):
        run_mock.side_effect = _disk_tool_stub()
        settings = dict(S3_SETTINGS)
        if immutability:
            settings["immutability"] = immutability
        repository = abr_v41.create_repository_record({"name": "ObjectRepo", "type": "s3", "s3": settings})
        for index in range(point_count):
            BACKUPS[f"point-{index}"] = {
                "id": f"point-{index}",
                "job_id": "job-1",
                "vm_id": "vm-1",
                "repository_id": repository["id"],
                "status": "success",
                "backup_type": "full",
                "created_at": "2026-01-01T00:00:00Z",
                "artifact": {
                    "disks": [{"abr_path": f"lab/vm-1/task-{index}", "abr_backup_name": f"disk-{index}"}]
                },
            }
        return repository["id"]

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_existing_backups_are_placed_under_the_current_setting(self, run_mock, _connect) -> None:
        repository_id = self._repository_with_points(run_mock, {"mode": "governance", "retention_days": 30})
        stub = _disk_tool_stub()
        run_mock.side_effect = stub

        result = abr_v41.apply_immutability_to_existing_backups(repository_id)

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["updated_restore_points"], 2)
        self.assertEqual(result["failed_restore_points"], 0)
        self.assertEqual(result["actual_retention_days"], [30])
        self.assertIn("for 30 day(s) (30 configured", result["message"])
        retention_calls = [command for command in stub.calls if command[0] == "retention"]
        self.assertEqual(len(retention_calls), 4)
        names = sorted(command[command.index("--name") + 1] for command in retention_calls)
        self.assertEqual(names, ["disk-0", "disk-1", "manifest", "manifest"])
        for command in retention_calls:
            self.assertEqual(command[command.index("--immutable") + 1], "governance")
            self.assertEqual(command[command.index("--retention") + 1], "30")

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_storage_refusal_is_reported_per_restore_point(self, run_mock, _connect) -> None:
        repository_id = self._repository_with_points(run_mock, {"mode": "compliance", "retention_days": 30})
        run_mock.side_effect = _disk_tool_stub(retention_error="InvalidRequest: object is already held")

        result = abr_v41.apply_immutability_to_existing_backups(repository_id)

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["updated_restore_points"], 0)
        self.assertEqual(result["failed_restore_points"], 2)
        self.assertTrue(all("already held" in f["message"] for f in result["failures"]))
        self.assertIn("for 30 day(s)", result["message"])

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_applying_needs_immutability_to_be_enabled(self, run_mock, _connect) -> None:
        repository_id = self._repository_with_points(run_mock, None)

        with self.assertRaises(ApiError) as error:
            abr_v41.apply_immutability_to_existing_backups(repository_id)
        self.assertIn("enable it before applying", error.exception.message)

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_repository_without_restore_points_reports_nothing_to_do(self, run_mock, _connect) -> None:
        repository_id = self._repository_with_points(
            run_mock, {"mode": "governance", "retention_days": 30}, point_count=0
        )
        run_mock.side_effect = _disk_tool_stub()

        result = abr_v41.apply_immutability_to_existing_backups(repository_id)

        self.assertEqual(result["status"], "success")
        self.assertIn("nothing to place under Object Lock", result["message"])


class S3ActualRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (JOBS, REPOSITORIES, TASKS):
            store.clear()

    def tearDown(self) -> None:
        for store in (JOBS, REPOSITORIES, TASKS):
            store.clear()

    def test_actual_retention_follows_the_job_when_it_is_longer(self) -> None:
        self.assertEqual(
            object_lock_actual_retention_days(
                {"retention_days": 13}, {"retention_days": 7}, block_generation_days=10
            ),
            23,
        )

    def test_the_configured_period_is_a_floor_rather_than_an_addend(self) -> None:
        # The chain needs the cycle plus the job retention; the configured period
        # only matters when it asks for longer than that.
        self.assertEqual(
            object_lock_actual_retention_days(
                {"retention_days": 3}, {"retention_days": 7}, block_generation_days=10
            ),
            13,
        )
        self.assertEqual(
            object_lock_actual_retention_days(
                {"retention_days": 3}, {"retention_days": 90}, block_generation_days=10
            ),
            90,
        )

    def test_minimum_only_scope_ignores_the_job_retention(self) -> None:
        self.assertEqual(
            object_lock_actual_retention_days(
                {"retention_days": 13},
                {"retention_days": 7, "retention_scope": "minimum_only"},
                block_generation_days=10,
            ),
            7,
        )

    def test_actual_retention_is_zero_when_immutability_is_disabled(self) -> None:
        self.assertEqual(
            object_lock_actual_retention_days({}, {"retention_days": 0}, block_generation_days=10), 0
        )

    def test_a_repository_stored_before_the_scope_existed_follows_the_job(self) -> None:
        legacy = {"mode": "governance", "retention_days": 7}
        self.assertEqual(
            object_lock_actual_retention_days({"retention_days": 13}, legacy, block_generation_days=10), 23
        )
        self.assertEqual(normalize_immutability_config(legacy)["retention_scope"], "job_retention")

    def test_retroactive_lock_uses_the_same_formula(self) -> None:
        JOBS["job-1"] = {"id": "job-1", "repo_id": "repo-1", "retention_days": 13}
        self.assertEqual(
            abr_v41.repository_actual_retention_days(JOBS["job-1"], {"retention_days": 7}), 23
        )

    def test_backup_run_locks_for_the_calculated_period_not_the_configured_one(self) -> None:
        REPOSITORIES["repo-1"] = {
            "id": "repo-1",
            "name": "ObjectRepo",
            "type": "s3",
            "metadata": {"s3": dict(S3_SETTINGS, immutability={"mode": "governance", "retention_days": 30})},
        }
        JOBS["job-1"] = {"id": "job-1", "repo_id": "repo-1", "retention_days": 45}

        repository = vcenter_backup._resolve_repository({"job_id": "job-1"})

        self.assertEqual(
            disk_tool_immutability_args(repository), ["--immutable", "governance", "--retention", "55"]
        )

    def test_repository_without_immutability_is_returned_untouched(self) -> None:
        REPOSITORIES["repo-1"] = {
            "id": "repo-1",
            "name": "ObjectRepo",
            "type": "s3",
            "metadata": {"s3": dict(S3_SETTINGS)},
        }
        JOBS["job-1"] = {"id": "job-1", "repo_id": "repo-1", "retention_days": 45}

        repository = vcenter_backup._resolve_repository({"job_id": "job-1"})

        self.assertEqual(disk_tool_immutability_args(repository), [])


class BackupDeleteCliTests(unittest.TestCase):
    def test_incomplete_delete_exits_non_zero(self) -> None:
        from python.cli.commands.backup_commands import BackupCommands
        from python.cli.exit_codes import INVALID_STATE

        payloads = {"deleted": False, "artifacts_removed": False, "immutable_restore_points": 1}
        with patch("python.cli.commands.backup_commands.CommandDependencies") as deps_mock:
            context = deps_mock.return_value.context
            context.api_client.delete.return_value = payloads
            code = BackupCommands().delete(
                argparse.Namespace(id="chain-1", force=False, base_url="http://x", output="table", log_level="info")
            )
        self.assertEqual(code, INVALID_STATE)

    def test_complete_delete_exits_zero(self) -> None:
        from python.cli.commands.backup_commands import BackupCommands

        with patch("python.cli.commands.backup_commands.CommandDependencies") as deps_mock:
            context = deps_mock.return_value.context
            context.api_client.delete.return_value = {"deleted": True, "artifacts_removed": True}
            code = BackupCommands().delete(
                argparse.Namespace(id="chain-1", force=False, base_url="http://x", output="table", log_level="info")
            )
        self.assertEqual(code, 0)


class BackupDeleteSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, JOBS, REPOSITORIES, TASKS):
            store.clear()

    def tearDown(self) -> None:
        for store in (BACKUPS, JOBS, REPOSITORIES, TASKS):
            store.clear()

    def _chain(self) -> str:
        BACKUPS["point-1"] = {
            "id": "point-1",
            "job_id": "job-1",
            "vm_id": "vm-1",
            "repository_id": "repo-1",
            "status": "success",
            "backup_type": "full",
            "execution_type": "Full",
            "created_at": "2026-01-01T00:00:00Z",
        }
        return str(abr_v41.normalize_backup_point(BACKUPS["point-1"])["chain_id"])

    @patch("python.srv.services.abr_v41._delete_backup_artifacts_from_point", return_value=True)
    def test_session_carries_the_delete_report_to_the_client(self, _delete) -> None:
        chain_id = self._chain()

        started = abr_v41.delete_backup_chain_request(chain_id, wait=True)
        session = abr_v41.session_to_dto(TASKS[started["session_id"]])

        self.assertEqual(session["type"], "delete")
        self.assertEqual(session["progress"], 100)
        self.assertTrue(session["delete"]["deleted"])
        self.assertEqual(session["delete"]["restore_point_count"], 1)

    @patch("python.srv.services.abr_v41._delete_backup_artifacts_from_point", return_value=True)
    def test_progress_is_reported_for_every_restore_point(self, _delete) -> None:
        chain_id = self._chain()
        seen: list[int] = []

        abr_v41.delete_backup_chain(chain_id, progress_callback=lambda update: seen.append(update["progress"]))

        self.assertEqual(seen[0], 0)
        self.assertEqual(seen[-1], 100)


class S3ImmutabilityWarningBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, JOBS, REPOSITORIES, TASKS):
            store.clear()

    def tearDown(self) -> None:
        for store in (BACKUPS, JOBS, REPOSITORIES, TASKS):
            store.clear()

    def _warnings(
        self, job_retention_days: int, immutability_days: int, scope: str = "job_retention"
    ) -> list[str]:
        REPOSITORIES["repo-1"] = {"id": "repo-1", "name": "R", "type": "s3", "metadata": {"s3": dict(S3_SETTINGS)}}
        JOBS["job-1"] = {
            "id": "job-1",
            "name": "Nightly",
            "job_type": "backup",
            "repo_id": "repo-1",
            "retention_days": job_retention_days,
        }
        return abr_v41._immutability_job_retention_warnings(
            "repo-1",
            {"mode": "governance", "retention_days": immutability_days, "retention_scope": scope},
        )

    def test_equal_periods_produce_no_warning(self) -> None:
        self.assertEqual(self._warnings(30, 30), [])

    def test_job_retention_longer_than_the_period_produces_no_warning(self) -> None:
        self.assertEqual(self._warnings(90, 30), [])

    def test_period_longer_than_the_job_retention_warns_about_the_cost(self) -> None:
        warnings = self._warnings(10, 30)
        self.assertTrue(any("the storage is paid for the whole lock" in w for w in warnings), warnings)

    def test_minimum_only_scope_warns_about_the_unprotected_gap(self) -> None:
        warnings = self._warnings(90, 7, scope="minimum_only")
        self.assertTrue(any("from day 7 to day 90" in w for w in warnings), warnings)

    def test_warning_counts_the_block_generation_period(self) -> None:
        warnings = self._warnings(10, 30)
        self.assertTrue(any("stay locked for 30 day(s)" in w for w in warnings), warnings)


class S3ImmutabilityBackupCommandTests(unittest.TestCase):
    def _command(self, repository: dict[str, object]) -> list[str]:
        return vcenter_backup._build_backup_command(
            tool_path=Path("/opt/altastor-backup/tools/abr-vmdk-tool/abr-vmdk-tool.sh"),
            backup_name="vm-1-disk-0",
            repository=repository,
            abr_path="lab/vm-1",
            datastore_path="[ds1] vm/vm.vmdk",
            host="vc.local",
            port=443,
            username="svc",
            password="secret",
            thumbprint="AA:BB",
            vm_moref="vm-1",
            snapshot_moref=None,
            transport_mode=None,
        )

    def test_backup_command_carries_the_repository_immutability(self) -> None:
        command = self._command(
            {
                "type": "s3",
                "s3": dict(S3_SETTINGS, immutability={"mode": "compliance", "retention_days": 45}),
            }
        )

        self.assertEqual(command[command.index("--immutable") + 1], "compliance")
        self.assertEqual(command[command.index("--retention") + 1], "45")

    def test_backup_command_omits_immutability_when_disabled(self) -> None:
        command = self._command({"type": "s3", "s3": dict(S3_SETTINGS)})

        self.assertNotIn("--immutable", command)
        self.assertNotIn("--retention", command)

    def test_local_repository_backup_command_has_no_immutability(self) -> None:
        command = self._command({"type": "local", "path": "/srv/backups"})

        self.assertNotIn("--immutable", command)


class S3ImmutabilityApiTests(unittest.TestCase):
    def setUp(self) -> None:
        from python.srv.api.auth_guard import require_session
        from python.srv.backup_server import app

        for store in (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS):
            store.clear()
        app.dependency_overrides[require_session] = lambda: {"user_id": "test", "user_name": "test"}
        self._app = app
        self._require_session = require_session
        self._client_cm = TestClient(app)
        self.client = self._client_cm.__enter__()

    def tearDown(self) -> None:
        self._client_cm.__exit__(None, None, None)
        self._app.dependency_overrides.pop(self._require_session, None)
        for store in (BACKUPS, HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS):
            store.clear()

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_test_immutability_endpoint(self, run_mock, _connect) -> None:
        run_mock.side_effect = _disk_tool_stub()

        response = self.client.post(
            "/api/v1/repositories/test-immutability",
            json={"s3": dict(S3_SETTINGS, immutability={"mode": "governance", "retention_days": 30})},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "success")
        self.assertIn("worm_enforcement", {check["name"] for check in body["checks"]})

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_test_immutability_endpoint_reports_an_incompatible_bucket(self, run_mock, _connect) -> None:
        run_mock.side_effect = _disk_tool_stub(supported_mode="none")

        response = self.client.post(
            "/api/v1/repositories/test-immutability",
            json={"s3": dict(S3_SETTINGS, immutability={"mode": "governance", "retention_days": 30})},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "error")

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_repository_create_and_verify_immutability_endpoints(self, run_mock, _connect) -> None:
        run_mock.side_effect = _disk_tool_stub()

        created = self.client.post(
            "/api/v1/repositories",
            json={
                "name": "ObjectRepo",
                "type": "s3",
                "s3": dict(S3_SETTINGS, immutability={"mode": "governance", "retention_days": 30}),
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        body = created.json()
        self.assertEqual(body["immutability"], {"mode": "governance", "retention_days": 30, "retention_scope": "job_retention"})
        self.assertFalse(body["immutability_effect"]["existing_data_protected"])

        verified = self.client.post(f"/api/v1/repositories/{body['id']}/verify-immutability")
        self.assertEqual(verified.status_code, 200, verified.text)
        self.assertEqual(verified.json()["status"], "success")

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_repository_create_rejects_an_incompatible_bucket(self, run_mock, _connect) -> None:
        run_mock.side_effect = _disk_tool_stub(supported_mode="none")

        created = self.client.post(
            "/api/v1/repositories",
            json={
                "name": "ObjectRepo",
                "type": "s3",
                "s3": dict(S3_SETTINGS, immutability={"mode": "governance", "retention_days": 30}),
            },
        )

        self.assertEqual(created.status_code, 422)
        self.assertEqual(created.json()["error"]["code"], "repository_immutability_unsupported")

    @patch("python.srv.services.abr_v41.s3_endpoint_connect_error", return_value=None)
    @patch("python.srv.core.storage_tool.run_disk_tool")
    def test_repository_patch_reports_the_immutability_effect(self, run_mock, _connect) -> None:
        run_mock.side_effect = _disk_tool_stub()
        created = self.client.post(
            "/api/v1/repositories", json={"name": "ObjectRepo", "type": "s3", "s3": dict(S3_SETTINGS)}
        ).json()

        patched = self.client.patch(
            f"/api/v1/repositories/{created['id']}",
            json={"s3": {"immutability": {"mode": "governance", "retention_days": 30}}},
        )

        self.assertEqual(patched.status_code, 200, patched.text)
        self.assertEqual(patched.json()["immutability"], {"mode": "governance", "retention_days": 30, "retention_scope": "job_retention"})
        self.assertTrue(patched.json()["immutability_effect"]["changed"])


class S3ImmutabilityCliTests(unittest.TestCase):
    def _parse(self, argv: list[str]):
        from python.cli.parsing.parser_builder import build_parser

        return build_parser().parse_args(argv)

    def test_repo_add_accepts_immutability_flags(self) -> None:
        args = self._parse(
            [
                "repo", "add", "--name", "S3-WORM", "--type", "s3",
                "--bucket", "altastor-objectlock", "--region", "us-east-1",
                "--immutable", "governance", "--immutability-days", "30",
            ]
        )

        repository_commands._validate_create_args(args)
        payload = repository_commands._build_s3_payload(args)

        self.assertEqual(payload["immutability"], {"mode": "governance", "retention_days": 30})

    def test_repo_add_accepts_the_retention_scope_flag(self) -> None:
        args = self._parse(
            [
                "repo", "add", "--name", "S3-WORM", "--type", "s3",
                "--bucket", "altastor-objectlock", "--region", "us-east-1",
                "--immutable", "governance", "--immutability-days", "30",
                "--immutability-retention", "minimum",
            ]
        )

        repository_commands._validate_create_args(args)

        self.assertEqual(
            repository_commands._build_s3_payload(args)["immutability"],
            {"mode": "governance", "retention_days": 30, "retention_scope": "minimum_only"},
        )

    def test_repo_add_rejects_a_retention_scope_without_immutability(self) -> None:
        args = self._parse(
            ["repo", "add", "--name", "S3", "--type", "s3", "--bucket", "b", "--region", "us-east-1",
             "--immutable", "none", "--immutability-retention", "job"]
        )

        with self.assertRaises(ValueError) as error:
            repository_commands._validate_create_args(args)

        self.assertIn("--immutability-retention requires", str(error.exception))

    def test_repo_add_without_immutability_flags_sends_nothing(self) -> None:
        args = self._parse(
            ["repo", "add", "--name", "S3", "--type", "s3", "--bucket", "b", "--region", "us-east-1"]
        )

        self.assertNotIn("immutability", repository_commands._build_s3_payload(args))

    def test_repo_add_rejects_a_mode_without_a_period(self) -> None:
        args = self._parse(
            ["repo", "add", "--name", "S3", "--type", "s3", "--bucket", "b", "--region", "us-east-1",
             "--immutable", "governance"]
        )

        with self.assertRaises(ValueError) as error:
            repository_commands._validate_create_args(args)
        self.assertIn("requires --immutability-days", str(error.exception))

    def test_repo_add_rejects_a_period_without_a_mode(self) -> None:
        args = self._parse(
            ["repo", "add", "--name", "S3", "--type", "s3", "--bucket", "b", "--region", "us-east-1",
             "--immutability-days", "30"]
        )

        with self.assertRaises(ValueError) as error:
            repository_commands._validate_create_args(args)
        self.assertIn("requires --immutable", str(error.exception))

    def test_repo_add_rejects_immutability_on_non_s3_repositories(self) -> None:
        args = self._parse(
            ["repo", "add", "--name", "Local", "--type", "local", "--path", "/srv/backups",
             "--immutable", "governance", "--immutability-days", "30"]
        )

        with self.assertRaises(ValueError) as error:
            repository_commands._validate_create_args(args)
        self.assertIn("only available for s3 repositories", str(error.exception))

    def test_repo_add_rejects_none_combined_with_a_period(self) -> None:
        args = self._parse(
            ["repo", "add", "--name", "S3", "--type", "s3", "--bucket", "b", "--region", "us-east-1",
             "--immutable", "none", "--immutability-days", "30"]
        )

        with self.assertRaises(ValueError):
            repository_commands._validate_create_args(args)

    def test_repo_edit_can_disable_immutability(self) -> None:
        args = self._parse(["repo", "edit", "--name", "S3-WORM", "--immutable", "none"])

        repository_commands._validate_edit_args({"type": "s3"}, args)

        self.assertEqual(
            repository_commands._build_s3_payload(args)["immutability"], {"mode": "none"}
        )

    def test_repo_edit_can_raise_only_the_period_of_an_immutable_repository(self) -> None:
        args = self._parse(["repo", "edit", "--name", "S3-WORM", "--immutability-days", "60"])
        current = {"type": "s3", "immutability": {"mode": "governance", "retention_days": 30}}

        repository_commands._validate_edit_args(current, args)

        self.assertEqual(repository_commands._build_s3_payload(args)["immutability"], {"retention_days": 60})

    def test_repo_edit_rejects_a_period_when_the_repository_has_no_mode(self) -> None:
        args = self._parse(["repo", "edit", "--name", "S3", "--immutability-days", "60"])
        current = {"type": "s3", "immutability": {"mode": "none", "retention_days": 0}}

        with self.assertRaises(ValueError) as error:
            repository_commands._validate_edit_args(current, args)
        self.assertIn("requires --immutable", str(error.exception))

    def test_repo_edit_can_switch_mode_keeping_the_stored_period(self) -> None:
        args = self._parse(["repo", "edit", "--name", "S3-WORM", "--immutable", "compliance"])
        current = {"type": "s3", "immutability": {"mode": "governance", "retention_days": 30}}

        repository_commands._validate_edit_args(current, args)

        self.assertEqual(repository_commands._build_s3_payload(args)["immutability"], {"mode": "compliance"})

    def test_verify_immutability_command_is_registered(self) -> None:
        args = self._parse(["repo", "verify-immutability", "--name", "S3-WORM"])

        self.assertEqual(args.handler.__name__, "verify_immutability")


if __name__ == "__main__":
    unittest.main()
