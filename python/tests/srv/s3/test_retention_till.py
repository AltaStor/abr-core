# SPDX-License-Identifier: Apache-2.0
# Copyright © 2026 Norvitex OÜ

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from python.srv.core import storage_tool
from python.srv.core.mongo_db_wrapper import BACKUPS, JOBS, REPOSITORIES
from python.srv.core.storage_tool import (
    RETENTION_TILL_KEY,
    apply_repository_immutability,
    chain_retention_till,
    copy_disk_tool_chain,
    disk_tool_supports_retention_till,
    retention_till_text,
    s3_block_generation_days,
)
from python.srv.plugins.vcenter import vcenter_backup
from python.srv.services import abr_v41
from python.srv.services.backup_mode import object_lock_expiry

FULL_STARTED = "2026-10-19T23:56:27Z"
INCREMENTAL_STARTED = "2026-10-20T00:00:14Z"
CHAIN = "backup::job-1::vm-1::repo-1"


def _repository(scope: str = "job_retention", days: int = 1) -> dict:
    return {
        "id": "repo-1",
        "name": "Locked",
        "type": "s3",
        "s3": {
            "bucket": "bucket",
            "region": "us-east-1",
            "immutability": {"mode": "governance", "retention_days": days, "retention_scope": scope},
        },
    }


def _job() -> dict:
    return {
        "id": "job-1",
        "repo_id": "repo-1",
        "retention_days": 3,
        "active_full_schedule": {
            "kind": "weekly",
            "enabled": True,
            "weekdays": ["Thu"],
            "time": "00:00",
            "timezone": "UTC",
        },
    }


def _task(started_at: str, backup_type: str) -> dict:
    return {
        "id": f"task-{backup_type}",
        "job_id": "job-1",
        "started_at": started_at,
        "parameters": {"vm_external_id": "vm-1", "resolved_backup_type": backup_type},
    }


def _command(repository: dict) -> list[str]:
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


def _lock(command: list[str]) -> list[str]:
    index = command.index("--immutable")
    return command[index : index + 4]


class ChainDateTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, JOBS, REPOSITORIES):
            store.clear()
        self.addCleanup(lambda: [store.clear() for store in (BACKUPS, JOBS, REPOSITORIES)])
        REPOSITORIES["repo-1"] = _repository()
        JOBS["job-1"] = _job()

    def _run(self, started_at: str, backup_type: str, *, supported: bool = True) -> tuple[list[str], dict]:
        task = _task(started_at, backup_type)
        repository = vcenter_backup._resolve_repository(task)
        with patch.object(vcenter_backup, "disk_tool_supports_retention_till", return_value=supported):
            command = _command(repository)
        plan = vcenter_backup._resolve_immutability_plan(task, REPOSITORIES["repo-1"])
        BACKUPS[f"point-{backup_type}"] = {
            "id": f"point-{backup_type}",
            "chain_id": CHAIN,
            "created_at": started_at,
            "immutability_generation_started_at": plan["generation_started_at"],
            "immutability_expires_at": plan["expires_at"],
        }
        return command, repository

    def _cycle_end(self) -> str:
        started = datetime.fromisoformat(FULL_STARTED.replace("Z", "+00:00"))
        expiry = object_lock_expiry(
            _job(), REPOSITORIES["repo-1"]["s3"]["immutability"],
            block_generation_days=s3_block_generation_days(), now=started,
        )
        return retention_till_text(expiry)

    def test_a_full_before_midnight_and_an_incremental_after_it_are_held_to_one_date(self) -> None:
        full, _ = self._run(FULL_STARTED, "activefull")
        incremental, _ = self._run(INCREMENTAL_STARTED, "incremental")

        self.assertEqual(_lock(full), ["--immutable", "governance", "--retention-till", self._cycle_end()])
        self.assertEqual(_lock(incremental), _lock(full))
        self.assertEqual(self._cycle_end(), "2026-10-25T00:00:00Z")
        self.assertNotIn("--retention", full + incremental)

    def test_the_catalog_records_the_date_the_storage_is_given(self) -> None:
        self._run(FULL_STARTED, "activefull")
        self._run(INCREMENTAL_STARTED, "incremental")

        recorded = {retention_till_text(datetime.fromisoformat(point["immutability_expires_at"])) for point in BACKUPS.values()}
        self.assertEqual(recorded, {self._cycle_end()})

    def test_the_manifest_of_each_backup_is_held_to_the_same_date(self) -> None:
        for started_at, backup_type in ((FULL_STARTED, "activefull"), (INCREMENTAL_STARTED, "incremental")):
            _, repository = self._run(started_at, backup_type)
            with (
                patch.object(storage_tool, "disk_tool_supports_retention_till", return_value=True),
                patch.object(storage_tool, "run_disk_tool_json", return_value={"status": "ok"}) as run,
            ):
                apply_repository_immutability(repository, "lab/vm-1", "manifest")

            command = run.call_args.args[0]
            with self.subTest(backup_type=backup_type):
                self.assertEqual(command[-2:], ["--retention-till", self._cycle_end()])

    def test_a_tool_without_the_date_option_still_gets_whole_days(self) -> None:
        full, repository = self._run(FULL_STARTED, "activefull", supported=False)

        self.assertEqual(_lock(full), ["--immutable", "governance", "--retention", "6"])
        with (
            patch.object(storage_tool, "disk_tool_supports_retention_till", return_value=False),
            patch.object(storage_tool, "run_disk_tool_json", return_value={"status": "ok"}) as run,
        ):
            apply_repository_immutability(repository, "lab/vm-1", "manifest")
        self.assertEqual(run.call_args.args[0][-2:], ["--retention", "6"])

    def test_the_minimum_scope_keeps_counting_days_from_each_backup(self) -> None:
        REPOSITORIES["repo-1"] = _repository(scope="minimum_only", days=7)

        full, repository = self._run(FULL_STARTED, "activefull")

        self.assertEqual(_lock(full), ["--immutable", "governance", "--retention", "7"])
        self.assertNotIn(RETENTION_TILL_KEY, repository)

    def test_a_grown_chain_date_extends_the_older_points_to_that_exact_date(self) -> None:
        self._run(FULL_STARTED, "activefull")
        BACKUPS["point-activefull"].update(
            {
                "repository_id": "repo-1",
                "segment_id": "segment-1",
                "artifact": {"disks": [{"abr_path": "lab/vm-1/full", "abr_backup_name": "vm-1-disk-0"}]},
            }
        )
        later = (datetime.now(timezone.utc) + timedelta(days=9, microseconds=500)).isoformat()

        with patch.object(abr_v41, "apply_repository_immutability", return_value={"status": "ok"}) as apply:
            abr_v41.extend_segment_object_lock("segment-1", expires_at=later)

        tills = {call.kwargs["retention_till"] for call in apply.call_args_list}
        self.assertEqual(len(apply.call_args_list), 2)
        self.assertEqual(tills, {retention_till_text(datetime.fromisoformat(later))})


class ApplyImmutabilityChainDateTests(unittest.TestCase):
    def setUp(self) -> None:
        for store in (BACKUPS, JOBS, REPOSITORIES):
            store.clear()
        self.addCleanup(lambda: [store.clear() for store in (BACKUPS, JOBS, REPOSITORIES)])
        REPOSITORIES["repo-1"] = _repository()
        JOBS["job-1"] = _job()
        for point_id, created_at, full in (("full", FULL_STARTED, True), ("incremental", INCREMENTAL_STARTED, False)):
            BACKUPS[point_id] = {
                "id": point_id,
                "job_id": "job-1",
                "vm_id": "vm-1",
                "repository_id": "repo-1",
                "status": "success",
                "full_backup": full,
                "created_at": created_at,
                "artifact": {"disks": [{"abr_path": f"lab/vm-1/{point_id}", "abr_backup_name": "vm-1-disk-0"}]},
            }

    def _apply(self, *, takes_date: bool = True) -> list:
        moment = datetime(2026, 10, 20, 23, 59, 50, tzinfo=timezone.utc)
        with (
            patch.object(abr_v41, "_now_utc", return_value=moment),
            patch.object(abr_v41, "disk_tool_supports_retention_till", return_value=takes_date),
            patch.object(abr_v41, "apply_repository_immutability", return_value={"status": "ok"}) as apply,
        ):
            result = abr_v41.apply_immutability_to_existing_backups("repo-1")
        self.assertEqual(result["status"], "success")
        return apply.call_args_list

    def test_every_object_of_the_chain_is_held_to_one_date_however_long_the_pass_takes(self) -> None:
        calls = self._apply()

        self.assertEqual(len(calls), 4)
        self.assertEqual({call.kwargs["retention_till"] for call in calls}, {"2026-10-25T00:00:00Z"})
        self.assertEqual(
            {BACKUPS[point]["immutability_expires_at"] for point in ("full", "incremental")},
            {"2026-10-25T00:00:00+00:00"},
        )

    def test_the_minimum_scope_is_still_counted_in_days_from_now(self) -> None:
        REPOSITORIES["repo-1"] = _repository(scope="minimum_only", days=7)

        calls = self._apply()

        self.assertEqual({call.kwargs["retention_till"] for call in calls}, {None})
        self.assertEqual({call.kwargs["retention_days"] for call in calls}, {7})

    def test_an_older_tool_gets_days_and_the_catalog_counts_them_from_now(self) -> None:
        calls = self._apply(takes_date=False)

        self.assertEqual({call.kwargs["retention_till"] for call in calls}, {None})
        self.assertEqual(
            {BACKUPS[point]["immutability_expires_at"][:10] for point in ("full", "incremental")},
            {(datetime(2026, 10, 20, 23, 59, 50, tzinfo=timezone.utc) + timedelta(days=calls[0].kwargs["retention_days"])).date().isoformat()},
        )


class RetentionTillTextTests(unittest.TestCase):
    def test_a_fraction_of_a_second_rounds_up_so_the_storage_never_holds_less(self) -> None:
        self.assertEqual(
            retention_till_text(datetime(2026, 10, 25, 0, 0, 0, 1, tzinfo=timezone.utc)), "2026-10-25T00:00:01Z"
        )

    def test_another_zone_is_written_in_utc(self) -> None:
        self.assertEqual(
            retention_till_text(datetime.fromisoformat("2026-10-25T02:00:00+02:00")), "2026-10-25T00:00:00Z"
        )

    def test_only_the_job_scope_of_an_enabled_s3_lock_gets_a_date(self) -> None:
        moment = "2026-10-25T00:00:00+00:00"
        disabled = _repository()
        disabled["s3"]["immutability"]["mode"] = "none"

        self.assertEqual(chain_retention_till(_repository(), moment), "2026-10-25T00:00:00Z")
        self.assertEqual(chain_retention_till(_repository(), "2026-10-25T00:00:00"), "2026-10-25T00:00:00Z")
        self.assertIsNone(chain_retention_till(_repository(scope="minimum_only"), moment))
        self.assertIsNone(chain_retention_till(disabled, moment))
        self.assertIsNone(chain_retention_till({"type": "local", "path": "/b"}, moment))
        self.assertIsNone(chain_retention_till(_repository(), ""))
        self.assertIsNone(chain_retention_till(_repository(), "not a date"))


class ToolSupportTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        storage_tool._retention_till_support.clear()
        self.addCleanup(storage_tool._retention_till_support.clear)

    def _launcher(self, help_text: str) -> Path:
        launcher = self.folder / "abr-vmdk-tool.sh"
        launcher.write_text(f"#!/bin/sh\necho '{help_text}'\n", encoding="utf-8")
        launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
        return launcher

    def test_a_tool_whose_help_names_the_option_takes_a_date(self) -> None:
        self.assertTrue(disk_tool_supports_retention_till(self._launcher("--retention-till DATE  Hold it until DATE")))

    def test_a_tool_whose_help_does_not_name_it_gets_days(self) -> None:
        self.assertFalse(disk_tool_supports_retention_till(self._launcher("--retention DAYS")))

    def test_a_missing_tool_gets_days(self) -> None:
        self.assertFalse(disk_tool_supports_retention_till(self.folder / "absent.sh"))

    def test_the_answer_is_asked_again_once_the_tool_is_replaced(self) -> None:
        launcher = self._launcher("--retention DAYS")
        self.assertFalse(disk_tool_supports_retention_till(launcher))

        self._launcher("--retention-till DATE")
        os.utime(launcher, ns=(1, 1))

        self.assertTrue(disk_tool_supports_retention_till(launcher))


class CopyAndExplicitPeriodTests(unittest.TestCase):
    def _copy(self, *, supported: bool) -> list[str]:
        with (
            patch.object(storage_tool, "disk_tool_supports_retention_till", return_value=supported),
            patch.object(storage_tool, "run_disk_tool", return_value=None) as run,
            patch.object(storage_tool, "parse_disk_tool_result", return_value={}),
        ):
            copy_disk_tool_chain(
                source_repository=_repository(),
                source_abr_path="lab/vm-1",
                source_names=["full", "incremental"],
                destination_repository=_repository(),
                destination_abr_path="lab/vm-1/synthetic",
                backup_name="vm-1-disk-0",
                retention_days=6,
                retention_till="2026-10-25T00:00:00Z",
            )
        return run.call_args.args[0]

    def test_a_synthetic_copy_is_held_to_the_chain_date(self) -> None:
        self.assertEqual(_lock(self._copy(supported=True)), ["--immutable", "governance", "--retention-till", "2026-10-25T00:00:00Z"])

    def test_a_synthetic_copy_by_an_older_tool_gets_whole_days(self) -> None:
        self.assertEqual(_lock(self._copy(supported=False)), ["--immutable", "governance", "--retention", "6"])

    def test_an_explicit_period_counted_from_now_stays_in_days(self) -> None:
        repository = dict(_repository(), **{RETENTION_TILL_KEY: "2026-10-25T00:00:00Z"})
        with (
            patch.object(storage_tool, "disk_tool_supports_retention_till", return_value=True),
            patch.object(storage_tool, "run_disk_tool_json", return_value={"status": "ok"}) as run,
        ):
            apply_repository_immutability(repository, "lab/vm-1", "manifest", retention_days=9)

        self.assertEqual(run.call_args.args[0][-2:], ["--retention", "9"])


if __name__ == "__main__":
    unittest.main()
