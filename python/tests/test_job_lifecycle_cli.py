# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from python.cli.cli_context import CliContext
from python.cli.cli_output_renderer import CliOutputRenderer
from python.cli.commands.job_commands import JobCommands
from python.cli.exit_codes import INVALID_STATE
from python.cli.parsing.parser_builder import AltaStorParserBuilder
from python.cli.services.job_service import JobService
from python.srv.core.job_scheduler import JobScheduler
from python.srv.core.mongo_db_wrapper import HYPERVISORS, JOBS, REPOSITORIES, RESOURCES_BY_HV, TASKS
from python.srv.core.storage_tool import SECRET_MASK
from python.srv.services import abr_v41
from python.srv.plugins.vcenter.vcenter_backup import DEFAULT_COMPRESSION_LEVEL


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


class _FakeApiClient:
    def get(self, path: str, params=None, timeout=None):  # noqa: ANN001
        return {"items": []}

    def post(self, path: str, json=None, params=None, timeout=None):  # noqa: ANN001
        raise AssertionError(f"Unexpected POST: {path}")

    def patch(self, path: str, json=None, params=None, timeout=None):  # noqa: ANN001
        raise AssertionError(f"Unexpected PATCH: {path}")


class JobLifecycleParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = AltaStorParserBuilder().build()

    def test_job_help_lists_lifecycle_subcommands(self) -> None:
        help_output = self._parse_help(["job", "--help"])
        for command in ("create", "create-copy", "list", "info", "edit", "start", "delete"):
            self.assertIn(command, help_output)

    def test_job_subcommands_support_help(self) -> None:
        for argv in (
            ["job", "--help"],
            ["job", "create", "--help"],
            ["job", "create-copy", "--help"],
            ["job", "list", "--help"],
            ["job", "info", "--help"],
            ["job", "edit", "--help"],
            ["job", "start", "--help"],
            ["job", "delete", "--help"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as ctx:
                    self.parser.parse_args(argv)
                self.assertEqual(ctx.exception.code, 0)

    def test_job_create_parser_accepts_supported_schedule_types(self) -> None:
        daily = self.parser.parse_args(
            ["job", "create", "--name", "Daily", "--vm", "vm-1", "--repository", "repo-1", "--schedule", "daily", "--time", "22:30"]
        )
        weekly = self.parser.parse_args(
            [
                "job",
                "create",
                "--name",
                "Weekly",
                "--vm",
                "vm-1",
                "--repository",
                "repo-1",
                "--schedule",
                "weekly",
                "--days",
                "mon,wed,fri",
                "--time",
                "23:00",
            ]
        )
        monthly = self.parser.parse_args(
            [
                "job",
                "create",
                "--name",
                "Monthly",
                "--vm",
                "vm-1",
                "--repository",
                "repo-1",
                "--schedule",
                "monthly",
                "--day-of-month",
                "1",
                "--time",
                "01:00",
            ]
        )
        periodic = self.parser.parse_args(
            ["job", "create", "--name", "Periodic", "--vm", "vm-1", "--repository", "repo-1", "--schedule", "periodic", "--every", "6h"]
        )

        self.assertEqual(daily.time, "22:30")
        self.assertEqual(weekly.days, ["monday", "wednesday", "friday"])
        self.assertEqual(monthly.day_of_month, 1)
        self.assertEqual(periodic.every, "6h")

    def test_job_create_parser_accepts_compression_and_encryption_flags(self) -> None:
        args = self.parser.parse_args(
            [
                "job",
                "create",
                "--name",
                "Encrypted",
                "--vm",
                "vm-1",
                "--repository",
                "repo-1",
                "--compression-level",
                "7",
                "--set-encryption",
                "--encryption-key",
                "s3cr3t-key",
            ]
        )

        self.assertEqual(args.compression_level, 7)
        self.assertTrue(args.set_encryption)
        self.assertFalse(args.reset_encryption)
        self.assertEqual(args.encryption_key, "s3cr3t-key")

    def test_job_create_parser_rejects_compression_level_out_of_range(self) -> None:
        with self.assertRaises(SystemExit):
            self.parser.parse_args(
                ["job", "create", "--name", "Bad", "--vm", "vm-1", "--repository", "repo-1", "--compression-level", "10"]
            )

    def test_job_edit_parser_rejects_combining_set_and_reset_encryption(self) -> None:
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["job", "edit", "--name", "Existing", "--set-encryption", "--reset-encryption"])

    def test_job_create_copy_parser_omits_compression_and_encryption_flags(self) -> None:
        help_output = self._parse_help(["job", "create-copy", "--help"])
        self.assertNotIn("--compression-level", help_output)
        self.assertNotIn("--set-encryption", help_output)
        self.assertNotIn("--encryption-key", help_output)

    def _parse_help(self, argv: list[str]) -> str:
        stream = io.StringIO()
        with redirect_stdout(stream):
            with self.assertRaises(SystemExit):
                self.parser.parse_args(argv)
        return stream.getvalue()


class JobPayloadBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        context = CliContext(base_url="http://example.test", output="json", api_client=_FakeApiClient(), output_renderer=CliOutputRenderer())
        self.service = JobService(context)

    def test_execution_schedule_payloads_build_both_full_schedules(self) -> None:
        args = SimpleNamespace(
            active_full_schedule="monthly",
            active_full_time="02:00",
            active_full_day_of_month=1,
            active_full_every=None,
            active_full_days=None,
            synthetic_full_schedule="weekly",
            synthetic_full_time="03:00",
            synthetic_full_day_of_month=None,
            synthetic_full_every=None,
            synthetic_full_days=["sunday"],
        )

        payloads = JobService.build_execution_schedule_payloads(args)

        self.assertEqual(
            payloads["active_full_schedule"],
            {"type": "monthly", "day_of_month": 1, "time": "02:00", "enabled": True},
        )
        self.assertEqual(
            payloads["synthetic_full_schedule"],
            {"type": "weekly", "days": ["sunday"], "time": "03:00", "enabled": True},
        )

    def test_execution_schedule_payloads_turn_off_a_schedule_with_none(self) -> None:
        args = SimpleNamespace(
            active_full_schedule="none",
            active_full_time=None,
            active_full_day_of_month=None,
            active_full_every=None,
            active_full_days=None,
            synthetic_full_schedule=None,
        )

        payloads = JobService.build_execution_schedule_payloads(args)

        self.assertEqual(payloads, {"active_full_schedule": None})

    def test_execution_schedule_state_flag_keeps_the_stored_settings(self) -> None:
        args = SimpleNamespace(
            active_full_schedule=None,
            active_full_time=None,
            active_full_day_of_month=None,
            active_full_every=None,
            active_full_days=None,
            active_full_enabled=False,
            active_full_disabled=True,
            synthetic_full_schedule=None,
        )
        current = {"active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "03:00", "enabled": True}}

        payloads = JobService.build_execution_schedule_payloads(args, current)

        self.assertEqual(
            payloads["active_full_schedule"],
            {"type": "weekly", "days": ["sunday"], "time": "03:00", "enabled": False},
        )

    def test_execution_schedule_state_flag_without_a_stored_schedule_is_rejected(self) -> None:
        args = SimpleNamespace(
            active_full_schedule=None,
            active_full_time=None,
            active_full_day_of_month=None,
            active_full_every=None,
            active_full_days=None,
            active_full_enabled=True,
            active_full_disabled=False,
            synthetic_full_schedule=None,
        )

        with self.assertRaises(ValueError):
            JobService.build_execution_schedule_payloads(args, {})

    def test_execution_schedule_errors_name_the_prefixed_flag(self) -> None:
        args = SimpleNamespace(
            active_full_schedule="weekly",
            active_full_time="03:00",
            active_full_day_of_month=None,
            active_full_every=None,
            active_full_days=None,
            synthetic_full_schedule=None,
        )

        with self.assertRaises(ValueError) as ctx:
            JobService.build_execution_schedule_payloads(args)

        self.assertEqual(str(ctx.exception), "Weekly schedules require --active-full-days")

    def test_create_backup_job_payload_supports_multiple_vms_and_daily_schedule(self) -> None:
        args = SimpleNamespace(
            name="Production VMs",
            vm=["vm-101", "vm-102"],
            vms=None,
            objects=None,
            inventory="vcenter-main",
            inventory_id=None,
            repository=None,
            repo="repo-local-01",
            repository_id=None,
            repo_id=None,
            schedule="daily",
            time="23:00",
            days=None,
            day_of_month=None,
            every=None,
            enabled=True,
            disabled=False,
            description="Critical nightly backups",
            retention_days=21,
            gfs_weekly=0,
            gfs_monthly=0,
            gfs_yearly=0,
            active_full_days=None,
            synthetic_full_days=None,
            mode=None,
            schedule_daily=None,
            gfs_policy=None,
        )

        payload = self.service.create_backup_job_payload(args)

        self.assertEqual(payload["vms"], ["vm-101", "vm-102"])
        self.assertEqual(payload["repo"], "repo-local-01")
        self.assertEqual(payload["schedule"], {"type": "daily", "time": "23:00", "enabled": True})
        self.assertEqual(payload["description"], "Critical nightly backups")
        self.assertEqual(payload["active_full_days"], [])
        self.assertEqual(payload["synthetic_full_days"], [])

    def test_create_backup_job_payload_passes_through_preparsed_full_days(self) -> None:
        args = SimpleNamespace(
            name="Production VMs",
            vm=["vm-101"],
            vms=None,
            objects=None,
            inventory="vcenter-main",
            inventory_id=None,
            repository=None,
            repo="repo-local-01",
            repository_id=None,
            repo_id=None,
            schedule="daily",
            time="23:00",
            days=None,
            day_of_month=None,
            every=None,
            enabled=True,
            disabled=False,
            description=None,
            retention_days=21,
            gfs_weekly=0,
            gfs_monthly=0,
            gfs_yearly=0,
            active_full_days=["Sun"],
            synthetic_full_days=["Mon", "Wed", "Fri"],
            mode=None,
            schedule_daily=None,
            gfs_policy=None,
        )

        payload = self.service.create_backup_job_payload(args)

        self.assertEqual(payload["active_full_days"], ["Sun"])
        self.assertEqual(payload["synthetic_full_days"], ["Mon", "Wed", "Fri"])

    def test_create_backup_job_payload_rejects_empty_vm_selection(self) -> None:
        args = SimpleNamespace(
            name="Empty",
            vm=[],
            vms="",
            objects=None,
            inventory="vcenter-main",
            inventory_id=None,
            repository=None,
            repo="repo-local-01",
            repository_id=None,
            repo_id=None,
            schedule=None,
            time=None,
            days=None,
            day_of_month=None,
            every=None,
            enabled=False,
            disabled=False,
            description=None,
            retention_days=14,
            gfs_weekly=0,
            gfs_monthly=0,
            gfs_yearly=0,
            active_full_days=None,
            synthetic_full_days=None,
            mode=None,
            schedule_daily=None,
            gfs_policy=None,
        )

        with self.assertRaisesRegex(ValueError, "at least one --vm"):
            self.service.create_backup_job_payload(args)

    @staticmethod
    def _base_create_args(**overrides: Any) -> SimpleNamespace:
        base = dict(
            name="Encrypted Job",
            vm=["vm-101"],
            vms=None,
            objects=None,
            inventory="vcenter-main",
            inventory_id=None,
            repository=None,
            repo="repo-local-01",
            repository_id=None,
            repo_id=None,
            schedule=None,
            time=None,
            days=None,
            day_of_month=None,
            every=None,
            enabled=False,
            disabled=False,
            description=None,
            retention_days=14,
            gfs_weekly=0,
            gfs_monthly=0,
            gfs_yearly=0,
            active_full_days=None,
            synthetic_full_days=None,
            compression_level=None,
            set_encryption=False,
            reset_encryption=False,
            encryption_key=None,
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_create_backup_job_payload_includes_compression_and_encryption_updates(self) -> None:
        args = self._base_create_args(compression_level=7, set_encryption=True, encryption_key="s3cr3t-key")

        payload = self.service.create_backup_job_payload(args)

        self.assertEqual(payload["task_parameters"], {"compression_level": 7, "encryption_enabled": True, "encryption_key": "s3cr3t-key"})

    def test_create_backup_job_payload_drops_reset_encryption_noop(self) -> None:
        args = self._base_create_args(reset_encryption=True)

        payload = self.service.create_backup_job_payload(args)

        self.assertNotIn("task_parameters", payload)

    def test_create_backup_job_payload_rejects_encryption_key_without_set_encryption(self) -> None:
        args = self._base_create_args(encryption_key="s3cr3t-key")

        with self.assertRaisesRegex(ValueError, "--encryption-key requires --set-encryption"):
            self.service.create_backup_job_payload(args)

    def test_create_backup_job_payload_rejects_set_encryption_without_key(self) -> None:
        args = self._base_create_args(set_encryption=True)

        with self.assertRaisesRegex(ValueError, "--set-encryption requires --encryption-key"):
            self.service.create_backup_job_payload(args)

    def test_create_backup_job_payload_sends_explicit_zero_retention_days_as_is(self) -> None:
        args = self._base_create_args(retention_days=0)

        payload = self.service.create_backup_job_payload(args)

        self.assertEqual(payload["retention_days"], 0)

    def test_edit_payload_can_toggle_existing_schedule_disabled(self) -> None:
        job = {
            "id": "job-1",
            "schedule": {"type": "weekly", "days": ["monday"], "time": "22:00", "enabled": True, "timezone": "UTC"},
        }
        args = SimpleNamespace(
            new_name=None,
            inventory=None,
            inventory_id=None,
            repository=None,
            repo=None,
            repository_id=None,
            repo_id=None,
            vm=[],
            vms=None,
            objects=None,
            schedule=None,
            time=None,
            days=None,
            day_of_month=None,
            every=None,
            enabled=False,
            disabled=True,
            description=None,
            retention_days=None,
            gfs_weekly=None,
            gfs_monthly=None,
            gfs_yearly=None,
            active_full_days=None,
            synthetic_full_days=None,
        )

        payload = self.service.build_edit_payload(job, args)

        self.assertEqual(payload["schedule"]["enabled"], False)
        self.assertEqual(payload["schedule"]["type"], "weekly")

    @staticmethod
    def _base_edit_args(**overrides: Any) -> SimpleNamespace:
        base = dict(
            new_name=None,
            inventory=None,
            inventory_id=None,
            repository=None,
            repo=None,
            repository_id=None,
            repo_id=None,
            vm=[],
            vms=None,
            objects=None,
            schedule=None,
            time=None,
            days=None,
            day_of_month=None,
            every=None,
            enabled=False,
            disabled=False,
            description=None,
            retention_days=None,
            gfs_weekly=None,
            gfs_monthly=None,
            gfs_yearly=None,
            active_full_days=None,
            synthetic_full_days=None,
            compression_level=None,
            set_encryption=False,
            reset_encryption=False,
            encryption_key=None,
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_edit_payload_supports_compression_level_update(self) -> None:
        args = self._base_edit_args(compression_level=9)

        payload = self.service.build_edit_payload({"id": "job-1"}, args)

        self.assertEqual(payload["task_parameters"], {"compression_level": 9})

    def test_edit_payload_reset_encryption_marks_keys_for_removal(self) -> None:
        args = self._base_edit_args(reset_encryption=True)

        payload = self.service.build_edit_payload({"id": "job-1"}, args)

        self.assertEqual(payload["task_parameters"], {"encryption_enabled": None, "encryption_key": None})

    def test_edit_payload_set_encryption_without_a_key_re_affirms_the_stored_one(self) -> None:
        args = self._base_edit_args(set_encryption=True)

        payload = self.service.build_edit_payload({"id": "job-1"}, args)

        self.assertEqual(payload["task_parameters"], {"encryption_enabled": True})

    def test_create_payload_still_requires_a_key_with_set_encryption(self) -> None:
        args = self._base_create_args(set_encryption=True)

        with self.assertRaisesRegex(ValueError, "requires --encryption-key"):
            self.service.create_backup_job_payload(args)

    def test_edit_payload_passes_through_preparsed_full_days(self) -> None:
        args = self._base_edit_args(active_full_days=["Sun"], synthetic_full_days=["Mon", "Wed"])

        payload = self.service.build_edit_payload({"id": "job-1"}, args)

        self.assertEqual(payload["active_full_days"], ["Sun"])
        self.assertEqual(payload["synthetic_full_days"], ["Mon", "Wed"])

    def test_edit_payload_gfs_disable_does_not_touch_counts(self) -> None:
        args = self._base_edit_args(gfs_disable=True)

        payload = self.service.build_edit_payload({"id": "job-1"}, args)

        self.assertEqual(payload, {"gfs_enabled": False})

    def test_edit_payload_gfs_enable_does_not_touch_counts(self) -> None:
        args = self._base_edit_args(gfs_enable=True)

        payload = self.service.build_edit_payload({"id": "job-1"}, args)

        self.assertEqual(payload, {"gfs_enabled": True})

    def test_edit_payload_omits_gfs_enabled_when_neither_flag_given(self) -> None:
        args = self._base_edit_args(gfs_weekly=5)

        payload = self.service.build_edit_payload({"id": "job-1"}, args)

        self.assertNotIn("gfs_enabled", payload)
        self.assertEqual(payload["gfs_weekly"], 5)

    def test_create_copy_payload_accepts_new_source_selector(self) -> None:
        args = SimpleNamespace(
            name="Copied Job",
            source=None,
            source_job="Existing Job",
            source_id=None,
            source_job_id=None,
            repository=None,
            repo=None,
            repository_id=None,
            repo_id=None,
            schedule=None,
            time=None,
            days=None,
            day_of_month=None,
            every=None,
            enabled=False,
            disabled=False,
            description=None,
            retention_days=None,
            gfs_weekly=None,
            gfs_monthly=None,
            gfs_yearly=None,
            synthetic_full_days=None,
            active_full_days=None,
        )

        payload = self.service.create_copy_job_payload(args)

        self.assertEqual(payload["name"], "Copied Job")
        self.assertEqual(payload["source"], "Existing Job")


class JobEngineLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        HYPERVISORS.clear()
        JOBS.clear()
        REPOSITORIES.clear()
        RESOURCES_BY_HV.clear()
        TASKS.clear()
        REPOSITORIES["repo-1"] = {"id": "repo-1", "name": "Repo One", "type": "local", "path": "/tmp/repo-1"}
        REPOSITORIES["repo-2"] = {"id": "repo-2", "name": "Repo Two", "type": "local", "path": "/tmp/repo-2"}
        self.inventory = abr_v41.create_inventory_record(
            {
                "name": "vcenter-main",
                "type": "hyper-v",
                "address": "192.168.10.10",
                "username": "administrator",
                "password": "secret",
            }
        )
        now = datetime.now(timezone.utc).isoformat()
        RESOURCES_BY_HV[self.inventory["id"]] = {
            ("virtual_machine", "vm-101"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-101",
                "name": "web-01",
                "payload": {"path": "/Production/web-01"},
                "discovered_at": now,
            },
            ("virtual_machine", "vm-102"): {
                "resource_type": "virtual_machine",
                "external_id": "vm-102",
                "name": "db-01",
                "payload": {"path": "/Production/db-01"},
                "discovered_at": now,
            },
        }

    def test_create_jobs_support_all_requested_schedule_types(self) -> None:
        daily = abr_v41.create_backup_job(
            {
                "name": "Daily Backup",
                "vms": ["web-01", "/Production/db-01"],
                "inventory_id": self.inventory["id"],
                "repo_id": "repo-1",
                "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"},
                "schedule": {"type": "daily", "time": "22:30", "enabled": True},
            }
        )
        weekly = abr_v41.create_backup_job(
            {
                "name": "Weekly Backup",
                "vms": ["web-01"],
                "inventory_id": self.inventory["id"],
                "repo_id": "repo-1",
                "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"},
                "schedule": {"type": "weekly", "days": ["monday", "friday"], "time": "23:00", "enabled": True},
            }
        )
        monthly = abr_v41.create_backup_job(
            {
                "name": "Monthly Backup",
                "vms": ["web-01"],
                "inventory_id": self.inventory["id"],
                "repo_id": "repo-1",
                "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"},
                "schedule": {"type": "monthly", "day_of_month": 1, "time": "01:00", "enabled": True},
            }
        )
        periodic = abr_v41.create_backup_job(
            {
                "name": "Periodic Backup",
                "vms": ["web-01"],
                "inventory_id": self.inventory["id"],
                "repo_id": "repo-1",
                "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"},
                "schedule": {"type": "periodic", "every": "6h", "enabled": True},
            }
        )

        self.assertEqual(daily["vm_count"], 2)
        self.assertEqual(weekly["schedule"]["type"], "weekly")
        self.assertEqual(monthly["schedule"]["day_of_month"], 1)
        self.assertEqual(periodic["schedule"]["every"], "6h")

    def test_create_copy_clones_existing_job_configuration(self) -> None:
        source = abr_v41.create_backup_job(
            {
                "name": "Source Job",
                "vms": ["web-01", "db-01"],
                "inventory_id": self.inventory["id"],
                "repo_id": "repo-1",
                "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"},
                "schedule": {"type": "weekly", "days": ["monday", "wednesday"], "time": "23:00", "enabled": False},
                "description": "Protect prod VMs",
                "task_parameters": {"tool_timeout_seconds": 600},
            }
        )

        copied = abr_v41.create_copy_job({"name": "Copied Job", "source_id": source["id"]})

        self.assertNotEqual(copied["id"], source["id"])
        self.assertEqual(copied["name"], "Copied Job")
        self.assertEqual(copied["inventory"], source["inventory"])
        self.assertEqual(copied["vm_ids"], source["vm_ids"])
        self.assertEqual(copied["repository"], source["repository"])
        self.assertEqual(copied["schedule"], source["schedule"])
        self.assertEqual(copied["enabled"], source["enabled"])
        self.assertEqual(copied["backup_options"], source["backup_options"])

    def test_edit_updates_schedule_and_repository(self) -> None:
        job = abr_v41.create_backup_job(
            {
                "name": "Editable Job",
                "vms": ["web-01"],
                "inventory_id": self.inventory["id"],
                "repo_id": "repo-1",
                "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"},
                "schedule": {"type": "daily", "time": "22:00", "enabled": True},
            }
        )

        updated = abr_v41.edit_job_record(
            job["id"],
            {
                "repo_id": "repo-2",
                "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"},
                "schedule": {"type": "weekly", "days": ["saturday", "sunday"], "time": "02:00", "enabled": False},
            },
        )

        self.assertEqual(updated["repository"]["id"], "repo-2")
        self.assertEqual(updated["schedule"]["type"], "weekly")
        self.assertEqual(updated["schedule"]["days"], ["saturday", "sunday"])
        self.assertFalse(updated["enabled"])
        self.assertEqual(updated["status"], "disabled")

    def test_gfs_disable_suspends_protection_without_discarding_configured_counts(self) -> None:
        job = abr_v41.create_backup_job(
            {
                "name": "GFS Job",
                "vms": ["web-01"],
                "inventory_id": self.inventory["id"],
                "repo_id": "repo-1",
                "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"},
                "gfs": {"weekly": 8, "monthly": 6, "yearly": 2},
            }
        )
        self.assertEqual(job["gfs"], {"weekly": 8, "monthly": 6, "yearly": 2, "enabled": True})

        disabled = abr_v41.edit_job_record(job["id"], {"gfs_enabled": False})
        self.assertEqual(disabled["gfs"], {"weekly": 8, "monthly": 6, "yearly": 2, "enabled": False})

        point = abr_v41.normalize_backup_point(
            {
                "id": "p1",
                "job_id": job["id"],
                "vm_id": "web-01",
                "repository_id": "repo-1",
                "execution_type": "ActiveFull",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        points = [point]
        abr_v41._apply_gfs_marks(points, dict(JOBS[job["id"]]["gfs"]))
        self.assertFalse(points[0]["gfs_protected"])

        # Re-enabling must restore protection without the administrator retyping counts.
        reenabled = abr_v41.edit_job_record(job["id"], {"gfs_enabled": True})
        self.assertEqual(reenabled["gfs"], {"weekly": 8, "monthly": 6, "yearly": 2, "enabled": True})
        abr_v41._apply_gfs_marks(points, dict(JOBS[job["id"]]["gfs"]))
        self.assertTrue(points[0]["gfs_protected"])

    def test_an_ad_hoc_full_never_takes_a_gfs_slot_from_a_scheduled_one(self) -> None:
        job = abr_v41.create_backup_job(
            {
                "name": "GFS Reasons Job",
                "vms": ["web-01"],
                "inventory_id": self.inventory["id"],
                "repo_id": "repo-1",
                "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"},
                "gfs": {"weekly": 4, "monthly": 0, "yearly": 0},
            }
        )

        def point(point_id: str, execution_type: str, reason: str, weeks_ago: int) -> dict:
            return abr_v41.normalize_backup_point(
                {
                    "id": point_id,
                    "job_id": job["id"],
                    "vm_id": "web-01",
                    "repository_id": "repo-1",
                    "execution_type": execution_type,
                    "execution_reason": reason,
                    "created_at": (
                        datetime.now(timezone.utc) - timedelta(weeks=weeks_ago)
                    ).isoformat(),
                }
            )

        points = [
            point("scheduled-active", "ActiveFull", "ActiveFullWindow", 3),
            point("scheduled-synthetic", "SyntheticFull", "SyntheticFullWindow", 2),
            point("manual-active", "ActiveFull", "ForceFull", 1),
            point("manual-synthetic", "SyntheticFull", "ForceSynthetic", 0),
        ]
        abr_v41._apply_gfs_marks(points, dict(JOBS[job["id"]]["gfs"]))
        protection = {item["id"]: bool(item["gfs_protected"]) for item in points}

        self.assertTrue(protection["scheduled-active"])
        self.assertTrue(protection["scheduled-synthetic"])
        self.assertFalse(protection["manual-active"])
        self.assertFalse(protection["manual-synthetic"])

    def test_gfs_count_edit_while_disabled_leaves_policy_suspended(self) -> None:
        job = abr_v41.create_backup_job(
            {
                "name": "GFS Job Two",
                "vms": ["web-01"],
                "inventory_id": self.inventory["id"],
                "repo_id": "repo-1",
                "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"},
                "gfs": {"weekly": 8, "monthly": 6, "yearly": 2},
            }
        )
        abr_v41.edit_job_record(job["id"], {"gfs_enabled": False})

        updated = abr_v41.edit_job_record(job["id"], {"gfs_weekly": 3})

        self.assertEqual(updated["gfs"], {"weekly": 3, "monthly": 6, "yearly": 2, "enabled": False})

    def test_create_job_defaults_compression_and_encryption_when_unset(self) -> None:
        job = abr_v41.create_backup_job(
            {"name": "Defaults Job", "vms": ["web-01"], "inventory_id": self.inventory["id"], "repo_id": "repo-1", "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"}}
        )

        self.assertEqual(job["compression_level"], DEFAULT_COMPRESSION_LEVEL)
        self.assertEqual(job["encryption"]["enabled"], False)
        self.assertEqual(job["encryption"]["key_set"], False)

    def test_create_job_stores_compression_and_masks_encryption_key(self) -> None:
        job = abr_v41.create_backup_job(
            {
                "name": "Encrypted Job",
                "vms": ["web-01"],
                "inventory_id": self.inventory["id"],
                "repo_id": "repo-1",
                "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"},
                "task_parameters": {"compression_level": 7, "encryption_enabled": True, "encryption_key": "s3cr3t-key"},
            }
        )

        self.assertEqual(job["compression_level"], 7)
        self.assertEqual(job["encryption"]["enabled"], True)
        self.assertEqual(job["encryption"]["key_set"], True)
        self.assertEqual(job["backup_options"]["encryption_key"], SECRET_MASK)
        self.assertEqual(job["task_parameters"]["encryption_key"], SECRET_MASK)

        # The raw key must still be present on the stored record for backup execution.
        stored = JOBS[job["id"]]
        self.assertEqual(stored["task_parameters"]["encryption_key"], "s3cr3t-key")

    def test_create_job_rejects_out_of_range_compression_level(self) -> None:
        with self.assertRaises(abr_v41.ApiError):
            abr_v41.create_backup_job(
                {
                    "name": "Bad Compression",
                    "vms": ["web-01"],
                    "inventory_id": self.inventory["id"],
                    "repo_id": "repo-1",
                    "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"},
                    "task_parameters": {"compression_level": 10},
                }
            )

    def test_create_job_refuses_encryption_without_a_key(self) -> None:
        before = len(JOBS)

        with self.assertRaises(abr_v41.ApiError) as ctx:
            abr_v41.create_backup_job(
                {
                    "name": "Default Encryption",
                    "vms": ["web-01"],
                    "inventory_id": self.inventory["id"],
                    "repo_id": "repo-1",
                    "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"},
                    "task_parameters": {"encryption_enabled": True},
                }
            )

        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(ctx.exception.code, "encryption_key_required")
        self.assertEqual(len(JOBS), before)

    def test_turning_encryption_on_by_edit_refuses_without_a_key(self) -> None:
        job = abr_v41.create_backup_job(
            {
                "name": "Later Encryption",
                "vms": ["web-01"],
                "inventory_id": self.inventory["id"],
                "repo_id": "repo-1",
                "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"},
            }
        )

        with self.assertRaises(abr_v41.ApiError) as ctx:
            abr_v41.edit_job_record(job["id"], {"task_parameters": {"encryption_enabled": True}})

        self.assertEqual(ctx.exception.status_code, 422)
        self.assertEqual(ctx.exception.code, "encryption_key_required")
        self.assertEqual(JOBS[job["id"]]["task_parameters"].get("encryption_enabled"), None)
        self.assertNotIn("encryption_key", JOBS[job["id"]]["task_parameters"])

    def test_reaffirming_encryption_without_a_key_keeps_the_stored_one(self) -> None:
        job = abr_v41.create_backup_job(
            {
                "name": "Reaffirmed Encryption",
                "vms": ["web-01"],
                "inventory_id": self.inventory["id"],
                "repo_id": "repo-1",
                "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"},
                "task_parameters": {"encryption_enabled": True, "encryption_key": "s3cr3t-key"},
            }
        )

        edited = abr_v41.edit_job_record(job["id"], {"task_parameters": {"encryption_enabled": True}})

        self.assertEqual(edited["encryption"]["enabled"], True)
        self.assertEqual(edited["encryption"]["key_set"], True)
        self.assertEqual(JOBS[job["id"]]["task_parameters"]["encryption_key"], "s3cr3t-key")

    def test_turning_encryption_off_drops_the_stored_key(self) -> None:
        job = abr_v41.create_backup_job(
            {
                "name": "Toggled Encryption",
                "vms": ["web-01"],
                "inventory_id": self.inventory["id"],
                "repo_id": "repo-1",
                "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"},
                "task_parameters": {"encryption_enabled": True, "encryption_key": "s3cr3t-key"},
            }
        )

        disabled = abr_v41.edit_job_record(
            job["id"], {"task_parameters": {"encryption_enabled": False, "encryption_key": None}}
        )

        self.assertEqual(disabled["encryption"]["enabled"], False)
        self.assertEqual(disabled["encryption"]["key_set"], False)
        self.assertNotIn("encryption_key", JOBS[job["id"]]["task_parameters"])

    def test_edit_job_updates_compression_level_and_sets_encryption(self) -> None:
        job = abr_v41.create_backup_job(
            {"name": "Editable Options Job", "vms": ["web-01"], "inventory_id": self.inventory["id"], "repo_id": "repo-1", "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"}}
        )

        updated = abr_v41.edit_job_record(job["id"], {"task_parameters": {"compression_level": 9}})
        self.assertEqual(updated["compression_level"], 9)
        self.assertEqual(updated["encryption"]["enabled"], False)
        self.assertEqual(updated["encryption"]["key_set"], False)

        encrypted = abr_v41.edit_job_record(
            job["id"],
            {"task_parameters": {"encryption_enabled": True, "encryption_key": "top-secret"}},
        )
        self.assertEqual(encrypted["encryption"]["enabled"], True)
        self.assertEqual(encrypted["encryption"]["key_set"], True)
        # Compression level set by the earlier edit must survive an unrelated update.
        self.assertEqual(encrypted["compression_level"], 9)

        reset = abr_v41.edit_job_record(job["id"], {"task_parameters": {"encryption_enabled": None, "encryption_key": None}})
        self.assertEqual(reset["encryption"]["enabled"], False)
        self.assertEqual(reset["encryption"]["key_set"], False)
        self.assertNotIn("encryption_key", JOBS[job["id"]]["task_parameters"])

    def test_start_job_forwards_compression_and_encryption_into_session_parameters(self) -> None:
        job = abr_v41.create_backup_job(
            {
                "name": "Wired Job",
                "vms": ["web-01"],
                "inventory_id": self.inventory["id"],
                "repo_id": "repo-1",
                "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"},
                "task_parameters": {"compression_level": 8, "encryption_enabled": True, "encryption_key": "hunter2-key"},
            }
        )

        abr_v41.start_job_record(job["id"], force_full=True)

        tasks = [task for task in TASKS.values() if task.get("job_id") == job["id"]]
        self.assertEqual(len(tasks), 1)
        parameters = tasks[0]["parameters"]
        self.assertEqual(parameters["compression_level"], 8)
        self.assertTrue(parameters["encryption_enabled"])
        self.assertEqual(parameters["encryption_key"], "hunter2-key")
        # Execution-resolved fields must win over anything stashed in task_parameters.
        self.assertEqual(parameters["resolved_backup_type"], "ActiveFull")

    def test_start_manual_run_works_when_schedule_is_disabled(self) -> None:
        job = abr_v41.create_backup_job(
            {
                "name": "Disabled Schedule Job",
                "vms": ["web-01"],
                "inventory_id": self.inventory["id"],
                "repo_id": "repo-1",
                "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"},
                "schedule": {"type": "daily", "time": "22:00", "enabled": False},
            }
        )

        started = abr_v41.start_job_record(job["id"], force_full=True)

        self.assertEqual(started["job"]["id"], job["id"])
        self.assertEqual(len(started["items"]), 1)
        self.assertEqual(started["items"][0]["resolved_backup_type"], "ActiveFull")

    def test_start_manual_run_advances_next_run_at_so_scheduler_does_not_double_dispatch(self) -> None:
        # Regression: a job's schedule can already be due (or overdue) at the
        # moment someone clicks "Run now"/"Run full backup" — e.g. an hourly
        # job the operator ran early. start_job_record() must push
        # next_run_at forward the same way the scheduler's own dispatch does,
        # or the very next scheduler poll sees the stale, still-due
        # next_run_at and fires the job again right behind the manual run.
        job = abr_v41.create_backup_job(
            {
                "name": "Overdue Periodic Job",
                "vms": ["web-01"],
                "inventory_id": self.inventory["id"],
                "repo_id": "repo-1",
                "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"},
                "schedule": {"type": "periodic", "every": "6h", "enabled": True},
            }
        )
        # Simulate the schedule already being due when the manual run happens.
        overdue_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        JOBS[job["id"]]["next_run_at"] = overdue_at

        started = abr_v41.start_job_record(job["id"])
        self.assertEqual(len(started["items"]), 1)

        # next_run_at must have moved into the future...
        next_run_at = datetime.fromisoformat(JOBS[job["id"]]["next_run_at"])
        self.assertGreater(next_run_at, datetime.now(timezone.utc))

        # ...so the scheduler's next poll must not enqueue a second run.
        scheduler = JobScheduler(poll_interval_seconds=0.1)
        scheduler._dispatch_due_jobs()
        tasks = [task for task in TASKS.values() if task.get("job_id") == job["id"]]
        self.assertEqual(len(tasks), 1)

    def test_delete_removes_job_record(self) -> None:
        job = abr_v41.create_backup_job(
            {
                "name": "Delete Me",
                "vms": ["web-01"],
                "inventory_id": self.inventory["id"],
                "repo_id": "repo-1",
                "active_full_schedule": {"type": "weekly", "days": ["sunday"], "time": "02:00"},
            }
        )

        deleted = abr_v41.delete_job_record(job["id"])

        self.assertTrue(deleted["deleted"])
        self.assertNotIn(job["id"], JOBS)


class JobCommandJsonOutputTests(unittest.TestCase):
    @patch("python.cli.api.client.requests.request")
    def test_json_output_works_for_create_list_info_start_and_delete(self, request_mock: object) -> None:
        request_mock.side_effect = [
            _FakeResponse(
                {
                    "id": "job-1",
                    "name": "Production VMs",
                    "job_type": "backup",
                    "inventory": {"id": "inv-1", "name": "vcenter-main"},
                    "repository": {"id": "repo-1", "name": "repo-local-01"},
                    "vm_ids": ["vm-101", "vm-102"],
                    "vm_names": ["vm-101", "vm-102"],
                    "vm_count": 2,
                    "schedule": {"type": "daily", "time": "23:00", "enabled": True, "timezone": "UTC"},
                    "schedule_summary": "Daily at 23:00 (UTC)",
                    "schedule_state": "enabled",
                    "enabled": True,
                    "status": "active",
                    "next_run_at": "2026-05-26T23:00:00+00:00",
                    "last_run_at": None,
                }
            ),
            _FakeResponse(
                {
                    "items": [
                        {
                            "id": "job-1",
                            "name": "Production VMs",
                            "job_type": "backup",
                            "inventory": {"id": "inv-1", "name": "vcenter-main"},
                            "repository": {"id": "repo-1", "name": "repo-local-01"},
                            "vm_ids": ["vm-101", "vm-102"],
                            "vm_names": ["vm-101", "vm-102"],
                            "vm_count": 2,
                            "schedule": {"type": "daily", "time": "23:00", "enabled": True, "timezone": "UTC"},
                            "schedule_summary": "Daily at 23:00 (UTC)",
                            "schedule_state": "enabled",
                            "enabled": True,
                            "status": "active",
                        }
                    ]
                }
            ),
            _FakeResponse(
                {
                    "id": "job-1",
                    "name": "Production VMs",
                    "job_type": "backup",
                    "inventory": {"id": "inv-1", "name": "vcenter-main"},
                    "repository": {"id": "repo-1", "name": "repo-local-01"},
                    "vm_ids": ["vm-101", "vm-102"],
                    "vm_names": ["vm-101", "vm-102"],
                    "vm_count": 2,
                    "schedule": {"type": "daily", "time": "23:00", "enabled": True, "timezone": "UTC"},
                    "schedule_summary": "Daily at 23:00 (UTC)",
                    "schedule_state": "enabled",
                    "enabled": True,
                    "status": "active",
                }
            ),
            _FakeResponse(
                {
                    "job": {"id": "job-1", "name": "Production VMs", "job_type": "backup"},
                    "items": [
                        {
                            "id": "sess-1",
                            "state": "queued",
                            "resolved_backup_type": "ActiveFull",
                            "resolved_backup_reason": "ForceFull",
                        }
                    ],
                }
            ),
            _FakeResponse({"id": "job-1", "deleted": True}),
        ]

        create_args = SimpleNamespace(
            base_url="http://example.test",
            output="json",
            name="Production VMs",
            inventory="vcenter-main",
            inventory_id=None,
            vm=["vm-101", "vm-102"],
            vms=None,
            objects=None,
            repo="repo-local-01",
            repo_id=None,
            schedule="daily",
            time="23:00",
            days=None,
            day_of_month=None,
            every=None,
            enabled=True,
            disabled=False,
            description=None,
            retention_days=14,
            gfs_weekly=0,
            gfs_monthly=0,
            gfs_yearly=0,
            synthetic_full_days=None,
            active_full_days=None,
            mode=None,
            schedule_daily=None,
            gfs_policy=None,
        )
        list_args = SimpleNamespace(base_url="http://example.test", output="json")
        info_args = SimpleNamespace(base_url="http://example.test", output="json", name=None, id="job-1")
        start_args = SimpleNamespace(base_url="http://example.test", output="json", name=None, id="job-1", full=True)
        delete_args = SimpleNamespace(base_url="http://example.test", output="json", name=None, id="job-1", force=True)

        outputs = []
        for command, args in (
            (JobCommands().create, create_args),
            (JobCommands().list, list_args),
            (JobCommands().info, info_args),
            (JobCommands().start, start_args),
            (JobCommands().delete, delete_args),
        ):
            stream = io.StringIO()
            with redirect_stdout(stream):
                command(args)
            outputs.append(json.loads(stream.getvalue()))

        self.assertEqual(outputs[0]["name"], "Production VMs")
        self.assertEqual(outputs[1]["items"][0]["vm_count"], 2)
        self.assertEqual(outputs[2]["id"], "job-1")
        self.assertEqual(outputs[3]["items"][0]["id"], "sess-1")
        self.assertTrue(outputs[4]["deleted"])


class JobStartWaitTests(unittest.TestCase):
    @patch("python.cli.api.client.requests.request")
    def test_start_without_wait_returns_the_start_response_unchanged(self, request_mock: object) -> None:
        request_mock.return_value = _FakeResponse(
            {"id": "sess-1", "state": "queued", "resolved_backup_type": "ActiveFull", "resolved_backup_reason": "ForceFull"}
        )
        args = SimpleNamespace(base_url="http://example.test", output="json", name=None, id="job-1", full=True)

        with patch("python.cli.commands.job_commands.watch_session") as watch_mock:
            stream = io.StringIO()
            with redirect_stdout(stream):
                exit_code = JobCommands().start(args)

        self.assertEqual(exit_code, 0)
        watch_mock.assert_not_called()
        self.assertEqual(json.loads(stream.getvalue())["state"], "queued")

    @patch("python.cli.api.client.requests.request")
    def test_start_with_wait_follows_the_new_session_to_completion(self, request_mock: object) -> None:
        request_mock.return_value = _FakeResponse(
            {
                "job": {"id": "job-1", "name": "Production VMs"},
                "items": [
                    {"id": "sess-1", "state": "queued", "resolved_backup_type": "ActiveFull", "resolved_backup_reason": "ForceFull"}
                ],
            }
        )
        args = SimpleNamespace(base_url="http://example.test", output="json", name=None, id="job-1", full=True, wait=True)

        with patch("python.cli.commands.job_commands.watch_session") as watch_mock:
            watch_mock.return_value = {"id": "sess-1", "state": "completed", "progress": 100}
            stream = io.StringIO()
            with redirect_stdout(stream):
                exit_code = JobCommands().start(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(watch_mock.call_args.args[1], "sess-1")
        self.assertEqual(json.loads(stream.getvalue())["state"], "completed")

    @patch("python.cli.api.client.requests.request")
    def test_start_with_wait_does_not_guess_which_session_to_follow_for_a_multi_vm_job(self, request_mock: object) -> None:
        request_mock.return_value = _FakeResponse(
            {
                "job": {"id": "job-1", "name": "Multi-VM job"},
                "items": [
                    {"id": "sess-1", "state": "queued"},
                    {"id": "sess-2", "state": "queued"},
                ],
            }
        )
        args = SimpleNamespace(base_url="http://example.test", output="table", name=None, id="job-1", full=False, wait=True)

        with patch("python.cli.commands.job_commands.watch_session") as watch_mock:
            stream_err = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(stream_err):
                exit_code = JobCommands().start(args)

        self.assertEqual(exit_code, 0)
        watch_mock.assert_not_called()
        self.assertIn("sess-1", stream_err.getvalue())
        self.assertIn("sess-2", stream_err.getvalue())

    @patch("python.cli.api.client.requests.request")
    def test_start_with_wait_returns_non_zero_when_the_followed_session_fails(self, request_mock: object) -> None:
        request_mock.return_value = _FakeResponse(
            {
                "job": {"id": "job-1", "name": "Production VMs"},
                "items": [{"id": "sess-1", "state": "queued"}],
            }
        )
        args = SimpleNamespace(base_url="http://example.test", output="json", name=None, id="job-1", full=False, wait=True)

        with patch("python.cli.commands.job_commands.watch_session") as watch_mock:
            watch_mock.return_value = {
                "id": "sess-1",
                "state": "failed",
                "message": "[Errno 13] Permission denied: '/mnt/backup/r1/vm/32fbe7ca'",
            }
            stream = io.StringIO()
            with redirect_stdout(stream):
                exit_code = JobCommands().start(args)

        self.assertEqual(json.loads(stream.getvalue())["state"], "failed")
        self.assertEqual(exit_code, INVALID_STATE)


if __name__ == "__main__":
    unittest.main()
